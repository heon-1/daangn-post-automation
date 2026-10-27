from .parallel import validate_batch_options


def resolve_run_options(args, config):
    """Explicit CLI options override the selected profile and its config defaults."""
    bulk = args.bulk
    workers = config.get("bulk_workers", 10) if bulk else config.get("workers", 3)
    lower = config.get("bulk_interval_min_seconds", 1) if bulk else config.get("interval_min_seconds", config["interval_seconds"])
    upper = config.get("bulk_interval_max_seconds", 3) if bulk else config.get("interval_max_seconds", max(lower, config["interval_seconds"] * 2))
    checkpoint_every = config.get("bulk_checkpoint_every", 10) if bulk else config.get("checkpoint_every", 1)
    options = {
        "limit": args.limit, "workers": args.workers if args.workers is not None else workers,
        "interval_min": args.interval_min if args.interval_min is not None else lower,
        "interval_max": args.interval_max if args.interval_max is not None else upper,
        "checkpoint_every": args.checkpoint_every if args.checkpoint_every is not None else checkpoint_every,
        "include_images": args.include_images if args.include_images is not None else config.get("include_images", True),
        "max_attempts": config["max_attempts"], "retry_delay": config["retry_delay_seconds"],
        "order": args.order, "seed": args.seed, "headless": args.headless,
    }
    validate_batch_options(options["limit"], options["workers"], options["max_attempts"],
                           options["interval_min"], options["interval_max"],
                           options["retry_delay"], options["order"], options["checkpoint_every"])
    return options
