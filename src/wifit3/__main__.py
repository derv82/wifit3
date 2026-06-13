"""Entry point for ``python -m wifit3`` and the ``wifit3`` console script."""


def main() -> None:
    # Import inside main(), not at module top: the WEP cracker's
    # ProcessPoolExecutor re-imports this module to spawn workers, which must
    # not drag in Textual + the whole UI just to run RC4 math.
    from wifit3.ui.app import WifiteApp

    WifiteApp().run()


if __name__ == "__main__":
    main()
