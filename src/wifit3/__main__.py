"""Entry point for ``python -m wifit3`` and the ``wifit3`` console script."""


def main() -> None:
    # Import inside main(), not at module top: the WEP cracker's
    # ProcessPoolExecutor re-imports this module to spawn workers, which must
    # not drag in Textual + the whole UI just to run RC4 math.
    from wifit3.ui.app import WifiteApp

    WifiteApp().run()


if __name__ == "__main__":
    # Frozen (PyInstaller) builds use the `spawn` start method, so each
    # ProcessPoolExecutor worker (the WEP cracker) re-execs this exe. freeze_support()
    # makes that re-exec run the worker bootstrap and exit, instead of launching a
    # second TUI. It is a no-op for normal `python -m wifit3` / console-script runs.
    import multiprocessing

    multiprocessing.freeze_support()
    main()
