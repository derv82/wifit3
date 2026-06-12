"""Entry point for ``python -m wifit3`` and the ``wifit3`` console script."""


def main() -> None:
    # `wifit3 --emit-udev`: print the blanket all-supported-cards Linux udev rules file (the
    # power-user manual-install path; the splash instead grants per-card OR all from its access
    # dialog). Handled before importing the UI so it works headless. [DEVICE-SETUP.md]
    import sys
    if "--emit-udev" in sys.argv[1:]:
        from wifit3.setup.linux import emit_udev_text
        sys.stdout.write(emit_udev_text())
        return

    # Import inside main(), not at module top: the WEP cracker's
    # ProcessPoolExecutor re-imports this module to spawn workers, which must
    # not drag in Textual + the whole UI just to run RC4 math.
    from wifit3.ui.app import WifiteApp

    WifiteApp().run()


if __name__ == "__main__":
    main()
