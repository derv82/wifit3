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


def test_realtek_define_is_not_empty():
    # rtw_mlme_ext.c:303-318 maps 0x7F to {WORLD, FCC1} and runs the
    # same null test, so it is valid, not empty.
    from wifit3.chips.rtl8188ftv_dkms import chplan
    assert chplan.is_empty(0x7F) is False
    assert chplan.is_valid(0x7F) is True
    plan, disable_sw, _ = chplan.config_channel_plan(
        b"\xff\xff", 0x7F, b"\xff\xff", chplan.RTW_CHPLAN_MAX,
        chplan.RTW_CHPLAN_WORLD_NULL, False)
    assert (plan, disable_sw) == (0x7F, False)


def test_init_channel_set_follows_plan_2g_index():
    # rtw_mlme_ext.c:146-156 RTW_ChannelPlan2G via RTW_ChannelPlanMap[plan].Index2G
    from wifit3.chips.rtl8188ftv_dkms import chplan
    assert chplan.init_channel_set(0x20) == list(range(1, 14))  # WORLD_NULL -> 2G_WORLD
    assert chplan.init_channel_set(0x22) == list(range(1, 12))  # FCC1_NULL -> 2G_FCC1
    assert chplan.init_channel_set(0x23) == list(range(1, 15))  # MKK1_NULL -> 2G_MKK1
    assert chplan.init_channel_set(0x24) == [10, 11, 12, 13]    # ETSI2_NULL -> 2G_ETSI2
    assert chplan.init_channel_set(0x7F) == list(range(1, 14))  # REALTEK_DEFINE -> 2G_WORLD


def test_init_channel_set_invalid_plan_is_empty():
    # rtw_mlme_ext.c:896-898: invalid plan returns chanset_size 0
    from wifit3.chips.rtl8188ftv_dkms import chplan
    assert chplan.init_channel_set(0x1A) == []
    assert chplan.init_channel_set(chplan.RTW_CHPLAN_MAX) == []
