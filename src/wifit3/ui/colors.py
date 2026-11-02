"""Colours with one meaning across screens."""

from wifit3.persist.ap_state import PasswordStatus

PASSWORD_MISMATCH_COLOR = "#ffaf00"     # a password newer evidence refutes, or a capture doesn't match

KEY_CHIP_STYLE = {
    PasswordStatus.CURRENT: "black bold on lightgreen",
    PasswordStatus.NOT_VERIFIED: "black bold on $warning",
    PasswordStatus.STALE: f"black bold on {PASSWORD_MISMATCH_COLOR}",
}
