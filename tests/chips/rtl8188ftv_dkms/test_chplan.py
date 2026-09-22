"""rtl8188ftv_dkms M2: channel-plan resolution (pure)."""


def test_hw_valid_plan_wins():
    from wifit3.chips.rtl8188ftv_dkms import chplan
    plan, disable_sw, country = chplan.config_channel_plan(
        b"\xff\xff", 0x20, b"\xff\xff", chplan.RTW_CHPLAN_MAX,
        chplan.RTW_CHPLAN_WORLD_NULL, False)
    assert (plan, disable_sw, country) == (0x20, False, None)


def test_ff_plan_falls_to_default():
    from wifit3.chips.rtl8188ftv_dkms import chplan
    plan, disable_sw, _ = chplan.config_channel_plan(
        b"\xff\xff", 0xFF, b"\xff\xff", chplan.RTW_CHPLAN_MAX,
        chplan.RTW_CHPLAN_WORLD_NULL, False)
    assert (plan, disable_sw) == (0x20, False)


def test_force_hw_bit():
    from wifit3.chips.rtl8188ftv_dkms import chplan
    plan, disable_sw, _ = chplan.config_channel_plan(
        b"\xff\xff", 0x80 | 0x20, b"\xff\xff", chplan.RTW_CHPLAN_MAX,
        chplan.RTW_CHPLAN_WORLD_NULL, False)
    assert (plan, disable_sw) == (0x20, True)


def test_country_lookup():
    from wifit3.chips.rtl8188ftv_dkms import chplan
    assert chplan.get_chplan_from_country(b"US") == 0x34
    assert chplan.get_chplan_from_country(b"us") == 0x34
    assert chplan.get_chplan_from_country(b"XX") is None


def test_hw_country_code_wins():
    from wifit3.chips.rtl8188ftv_dkms import chplan
    plan, _, country = chplan.config_channel_plan(
        b"US", 0x20, b"\xff\xff", chplan.RTW_CHPLAN_MAX,
        chplan.RTW_CHPLAN_WORLD_NULL, False)
    assert (plan, country) == (0x34, b"US")


def test_validity():
    from wifit3.chips.rtl8188ftv_dkms import chplan
    assert chplan.is_valid(0x20) is True
    assert chplan.is_valid(0xFF) is False
    assert chplan.is_valid(chplan.RTW_CHPLAN_MAX) is False
