#!/usr/bin/env python3 -u
#
# Copyright (c) 2019-present, Zhiqiang Wang.

"""
Recognize pre-processed image with a trained model.
"""

import torch
try:
    from torch.hub import load_state_dict_from_url
except ImportError:
    from torch.utils.model_zoo import load_url as load_state_dict_from_url

from fairseq import options, tasks, progress_bar, utils
from fairseq.meters import StopwatchMeter, TimeMeter

from sightseq.coco_eval import CocoEvaluator


def main(args):
    utils.import_user_module(args)

    print(args)

    use_cuda = torch.cuda.is_available() and not args.cpu

    # Load dataset split
    task = tasks.setup_task(args)
    task.load_dataset(args.valid_subset)

    # Build model and criterion
    model = task.build_model(args)

    model_path = model.hub_models()[args.arch]
    # Load ensemble
    print('| loading model(s) from {}'.format(model_path))

    # load checkpoints
    model_state_dict = load_state_dict_from_url(model_path)
    model.load_state_dict(model_state_dict, strict=True)

    models = [model]
    # Optimize ensemble for generation
    # model.make_generation_fast_()
    if args.fp16:
        model.half()
    if use_cuda:
        model.cuda()

    # Load dataset (possibly sharded)
    itr = task.get_batch_iterator(
        dataset=task.dataset(args.valid_subset),
        max_tokens=args.max_tokens,
        max_sentences=args.max_sentences,
        max_positions=utils.resolve_max_positions(
            task.max_positions(),
            *[model.max_positions() for model in models]
        ),
        ignore_invalid_inputs=args.skip_invalid_size_inputs_valid_test,
        required_batch_size_multiple=args.required_batch_size_multiple,
        num_shards=args.distributed_world_size,
        shard_id=args.distributed_rank,
        num_workers=args.num_workers,
    ).next_epoch_itr(shuffle=False)

    # Initialize generator
    gen_timer = StopwatchMeter()
    generator = task.build_generator(args)

    # Generate and compute score
    coco = task.dataset(args.valid_subset).coco
    iou_types = ['bbox']
    scorer = CocoEvaluator(coco, iou_types)

    num_images = 0

    with progress_bar.build_progress_bar(
        args, itr,
        prefix='inference on \'{}\' subset'.format(args.valid_subset),
        no_progress_bar='simple',
    ) as progress:
        wps_meter = TimeMeter()
        for sample in progress:
            sample = utils.move_to_cuda(sample) if use_cuda else sample
            gen_timer.start()
            hypos = task.inference_step(generator, models, sample)
            num_generated_boxes = sum(len(h['scores']) for h in hypos)
            gen_timer.stop(num_generated_boxes)

            result = {}
            for i, sample_id in enumerate(sample['id'].tolist()):
                result[sample_id] = hypos[i]

            scorer.update(result)

            wps_meter.update(num_generated_boxes)
            progress.log({'wps': round(wps_meter.avg)})
            num_images += sample['nsentences']

    print('| Detected {} images ({} tokens) in {:.1f}s ({:.2f} images/s, {:.2f} tokens/s)'.format(
        num_images, gen_timer.n, gen_timer.sum, num_images / gen_timer.sum, 1. / gen_timer.avg))

    # gather the stats from all processes
    scorer.synchronize_between_processes()
    # accumulate predictions from all images
    scorer.accumulate()
    scorer.summarize()

    return scorer


def cli_main():
    parser = options.get_training_parser()
    args = options.parse_args_and_arch(parser)
    main(args)


if __name__ == '__main__':
    cli_main()
