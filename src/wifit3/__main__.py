"""Entry point for ``python -m wifit3`` and the ``wifit3`` console script."""


def main() -> None:
    # Import inside main(), not at module top: the WEP cracker's
    # ProcessPoolExecutor re-imports this module to spawn workers, which must
    # not drag in Textual + the whole UI just to run RC4 math.
    import logging
    import time

    _t0 = time.perf_counter()
    from wifit3.ui.app import WifiteApp

    app = WifiteApp()  # configures WIFIT3_LOG file logging
    logging.getLogger("wifit3.startup").debug(
        "startup: import WifiteApp + app init took %.0f ms", (time.perf_counter() - _t0) * 1000)
    app.run()


if __name__ == "__main__":
    main()
