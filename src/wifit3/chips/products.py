from enum import StrEnum


class ProductName(StrEnum):
    pass


class AboCom(ProductName):
    AU7212    = "AboCom AU7212"
    BGN_MINI  = "AboCom BGN Mini"
    _802_11AC = "AboCom 802.11ac"


class Acer(ProductName):
    WAVE_D7 = "Acer Wave D7"


class AirLive(ProductName):
    WN_370USB = "WN-370USB"


class AirTies(ProductName):
    USB2 = "AirTies USB2"  # https://linux-hardware.org/?id=usb:1eda-2315


class ALFA(ProductName):
    AWUS036ACH   = "AWUS036ACH"
    AWUS036ACHM  = "AWUS036ACHM"
    AWUS036ACM   = "AWUS036ACM"
    AWUS036ACS   = "AWUS036ACS"
    AWUS036AXML  = "AWUS036AXML"
    AWUS036H     = "AWUS036H"
    AWUS036NH    = "AWUS036NH"
    AWUS036NHA   = "AWUS036NHA"
    AWUS051NH_V2 = "AWUS051NH v2"
    AWUS1900     = "AWUS1900"


class Altai(ProductName):
    WA1011N_GU = "Altai WA1011N-GU"


class AMax(ProductName):
    _54MBPS  = "A-Max 54Mbps"
    _802_11G = "A-Max 802.11g"


class AmpedWireless(ProductName):
    ACA1 = "Amped ACA1"


class ASUS(ProductName):
    USB_AC50      = "ASUS USB-AC50"
    USB_AC51      = "ASUS USB-AC51"
    USB_AC53_NANO = "USB-AC53 Nano"
    USB_AC54      = "ASUS USB-AC54"
    USB_AC55      = "ASUS USB-AC55"
    USB_AC55_B1   = "ASUS USB-AC55 B1"
    USB_AC56      = "ASUS USB-AC56"
    USB_AC58_A1   = "ASUS USB-AC58-A1"
    USB_AC68      = "ASUS USB-AC68"
    USB_BE92      = "ASUS USB-BE92"
    USB_BE92_NANO = "USB-BE92 Nano"
    USB_BE93      = "ASUS USB-BE93"
    USB_N14       = "ASUS USB-N14"
    USB_N53_B1    = "ASUS USB-N53 B1"
    WL_167G       = "ASUS WL-167g"
    W_LINK        = "ASUS W.Link"  # https://linux-hardware.org/?id=usb:0b05-171d
    _8822BU_1870  = "ASUS 8822BU-1870"
    _8822BU_1874  = "ASUS 8822BU-1874"


class Aukey(ProductName):
    USB_AC1200 = "Aukey USB-AC1200"


class Auscoumer(ProductName):
    _600 = "Auscoumer 600"


class AVM(ProductName):
    FRITZ_WLAN_AC430 = "FRITZ!WLAN AC430"
    FRITZ_WLAN_AC860 = "FRITZ!WLAN AC860"
    FRITZ_WLAN_N_V2  = "FRITZ!WLAN N v2"


class Belkin(ProductName):
    F5D7050   = "F5D7050"
    F9L1106V1 = "Belkin F9L1106v1"  # https://linux-hardware.org/?id=usb:050d-1106
    F9L1109V1 = "Belkin F9L1109v1"  # https://linux-hardware.org/?id=usb:050d-1109


class Buffalo(ProductName):
    WI_FI          = "Nintendo Wi-Fi"
    WI_U2_433DHP   = "WI-U2-433DHP"  # https://linux-hardware.org/?id=usb:0411-029b
    WI_U2_433DM    = "WI-U2-433DM"
    WI_U2_866DM    = "WI-U2-866DM"
    WI_U3_866D     = "WI-U3-866D"  # https://linux-hardware.org/?id=usb:0411-025d
    WI_U3_866DHP   = "WI-U3-866DHP"
    WLI_U2_KG54    = "WLI-U2-KG54"
    WLI_U2_KG54_AI = "WLI-U2-KG54-AI"
    WLI_U2_KG54_BB = "WLI-U2-KG54-BB"
    WLI_U2_KG54_YB = "WLI-U2-KG54-YB"
    _03EF          = "Buffalo 03EF"


class CCandC(ProductName):
    _433MBPS = "CC&C 433Mbps"


class CNet(ProductName):
    CWD_8554 = "CNet CWD-8554"  # https://linux-hardware.org/?id=usb:1371-9401


class Comcast(ProductName):
    KXW02AAA = "Xfinity KXW02AAA"


class Comfast(ProductName):
    CF_953AX = "CF-953AX"


class Devolo(ProductName):
    STICK = "Devolo Stick"


class DLink(ProductName):
    AC1200          = "D-Link AC1200"
    DWA_121B1       = "D-Link DWA-121B1"  # https://linux-hardware.org/?id=usb:2001-331b
    DWA_123_REV_D1  = "DWA-123 rev.D1"
    DWA_125_REV_D1  = "DWA-125 rev.D1"
    DWA_126         = "D-Link DWA-126"
    DWA_137         = "D-Link DWA-137"
    DWA_140_REV_B3  = "DWA-140 rev.B3"
    DWA_171C        = "D-Link DWA-171C"
    DWA_171_REV_A1  = "DWA-171 rev.A1"
    DWA_171_REV_B   = "DWA-171 rev.B"
    DWA_172         = "D-Link DWA-172"
    DWA_181         = "D-Link DWA-181"
    DWA_182         = "D-Link DWA-182"  # https://linux-hardware.org/?id=usb:2001-3315
    DWA_182_D1      = "DWA-182 D1"
    DWA_182_REV_B   = "DWA-182 rev.B"
    DWA_183         = "D-Link DWA-183"  # https://linux-hardware.org/?id=usb:2001-330e
    DWA_183_D       = "D-Link DWA-183 D"
    DWA_192         = "D-Link DWA-192"
    DWA_T185_REV_A1 = "DWA-T185 rev.A1"
    DWL_G122_REV_B1 = "DWL-G122 rev.B1"
    GO_N150_REV_B1  = "GO-N150 rev.B1"


class Edimax(ProductName):
    EW_7711MAC    = "EW-7711MAC"
    EW_7722UAC    = "EW-7722UAC"
    EW_7811UAC    = "EW-7811UAC"
    EW_7811UCB    = "EW-7811UCB"
    EW_7811UN_V2  = "EW-7811UN v2"
    EW_7811UTC    = "EW-7811UTC"
    EW_7811UTC_AC = "EW-7811UTC/AC"
    EW_7822UAC    = "EW-7822UAC"
    EW_7822UAD    = "EW-7822UAD"
    EW_7822ULC    = "EW-7822ULC"
    EW_7822UTC    = "EW-7822UTC"
    EW_7833UAC    = "EW-7833UAC"
    _8811CU       = "Edimax 8811CU"


class EDUP(ProductName):
    EP_BE1703S = "EP-BE1703S"


class Elecom(ProductName):
    GENERIC       = "Elecom Generic"
    LD_USB20      = "LD-USB20"
    WDB_433SU2M2  = "WDB-433SU2M2"
    WDB_867DU3S   = "WDB-867DU3S"
    WDC_150SU2M   = "WDC-150SU2M"
    WDC_1300SU2   = "WDC-1300SU2"
    WDC_1300SU3   = "WDC-1300SU3"
    WDC_433DU2HBK = "WDC-433DU2HBK"
    WDC_433SU2M2  = "WDC-433SU2M2"
    WDC_BE28TU3_B = "WDC-BE28TU3-B"


class EnGenius(ProductName):
    EUB1200AC = "EUB1200AC"


class Gigabyte(ProductName):
    GN_54G  = "Gigabyte GN-54G"
    GN_WBKG = "Gigabyte GN-WBKG"


class Hawking(ProductName):
    HD65U_22 = "Hawking HD65U-22"
    HD65U_23 = "Hawking HD65U-23"
    HW12ACU  = "Hawking HW12ACU"
    HW17ACU  = "Hawking HW17ACU"


class Hercules(ProductName):
    HWGUSB2_54 = "HWGUSB2-54"


class HighCloud(ProductName):
    HC_M7662BU1 = "HC-M7662BU1"


class IMC(ProductName):
    AR9271_R28 = "IMC AR9271 r28"
    AR9271_R48 = "IMC AR9271 r48"
    AR9271_R49 = "IMC AR9271 r49"
    AR9271_R50 = "IMC AR9271 r50"
    AW_NU137   = "IMC AW-NU137"  # https://linux-hardware.org/?id=usb:13d3-3327
    UB93       = "IMC UB93"


class IODATA(ProductName):
    WN_AC433UK = "I-O WN-AC433UK"
    WN_AC867U  = "I-O WN-AC867U"


class Linksys(ProductName):
    AE6000      = "Linksys AE6000"
    HU200TS     = "Linksys HU200TS"
    WUSB54GP_V4 = "WUSB54GP v4"
    WUSB54G_V4  = "WUSB54G v4"
    WUSB6300    = "Linksys WUSB6300"
    WUSB6300_V2 = "WUSB6300 v2"
    WUSB6400M   = "WUSB6400M"


class LiteOn(ProductName):
    AR9271  = "Lite-On AR9271"
    WN4516R = "LiteOn WN4516R"
    WN4519R = "LiteOn WN4519R"
    WN8602L = "LiteOn WN8602L"


class Logitec(ProductName):
    AC866   = "Logitec AC866"
    RTL8187 = "Logitec RTL8187"


class LOTEKOO(ProductName):
    _150MBPS = "LOTEKOO 150Mbps"


class MediaTek(ProductName):
    MT7925U = "MediaTek MT7925U"


class Mercury(ProductName):
    UD13 = "Mercury UD13"


class Mercusys(ProductName):
    MA30H = "Mercusys MA30H"
    MA30N = "Mercusys MA30N"


class Microsoft(ProductName):
    ADAPTER_E6 = "Xbox1 Adapter-e6"
    ADAPTER_FE = "Xbox1 Adapter-fe"


class MSI(ProductName):
    MS_6861 = "MSI MS-6861"
    MS_6865 = "MSI MS-6865"
    MS_6869 = "MSI MS-6869"


class NEC(ProductName):
    ATERMWL900U = "AtermWL900U"


class Netgear(ProductName):
    A6100       = "Netgear A6100"
    A6150       = "Netgear A6150"
    A6200_V2    = "Netgear A6200 v2"
    A6210       = "Netgear A6210"
    A7000       = "Netgear A7000"  # https://wikidevi.wi-cat.ru/Netgear_A7000
    A7500       = "Netgear A7500"  # https://wikidevi.wi-cat.ru/Netgear_A9000 (mentioned)
    A8000       = "Netgear A8000"  # https://wikidevi.wi-cat.ru/Netgear_A8000
    A9000       = "Netgear A9000"  # https://wikidevi.wi-cat.ru/Netgear_A9000
    N150        = "Netgear N150"
    RTL8187     = "Netgear RTL8187"
    WG111_V1_V2 = "WG111 v1/v2"
    WNDA3100V3  = "WNDA3100v3"


class NovaTech(ProductName):
    NV_902W = "NV-902W"


class Obihai(ProductName):
    OBIWIFI = "Obihai OBiWiFi"


class Panda(ProductName):
    PAU05_06   = "Panda PAU05/06"
    PAU09_N600 = "Panda PAU09 N600"
    PAU0B      = "Panda PAU0B"
    PAU0F      = "Panda PAU0F"


class Philips(ProductName):
    PTA01 = "PTA01"


class Planex(ProductName):
    GW_450D = "Planex GW-450D"
    GW_450S = "Planex GW-450S"
    GW_900D = "GW-900D"


class Qcom(ProductName):
    _54G = "Qcom 54G"  # https://linux-hardware.org/?id=usb:18e8-6232


class Ralink(ProductName):
    C54RUV2 = "Ralink C54RUv2"  # https://linux-hardware.org/?id=usb:14b2-3c02


class Realtek(ProductName):
    _8818EUS = "Realtek 8818EUS"


class Sagem(ProductName):
    WIFI_11G = "Sagem WiFi 11g"


class Siemens(ProductName):
    _54G = "Siemens 54G"


class Sitecom(ProductName):
    AC1200   = "Sitecom AC1200"
    N150_V2  = "Sitecom N150 v2"
    WL3001   = "Sitecom WL3001"
    WLA_3100 = "Sitecom WLA-3100"


class Spairon(ProductName):
    UB801R = "Spairon UB801R"


class SureCom(ProductName):
    EP_9001G = "Surecom EP-9001G"
    RT2570   = "SureCom RT2570"


class Tenda(ProductName):
    U12 = "Tenda U12"
    U9  = "Tenda U9"


class TOTOLINK(ProductName):
    A650UA_V3 = "A650UA v3"


class TPLink(ProductName):
    ARCHER_T1U       = "Archer T1U"      # https://wikidevi.wi-cat.ru/TP-LINK_Archer_T1U
    ARCHER_T2U       = "Archer T2U"      # https://wikidevi.wi-cat.ru/TP-LINK_Archer_T2U
    ARCHER_T2U_V2    = "Archer T2U v2"   # https://wikidevi.wi-cat.ru/TP-LINK_Archer_T2U_v2
    ARCHER_T2U_V3    = "Archer T2U v3"   # https://wikidevi.wi-cat.ru/TP-LINK_Archer_T2U_v3
    ARCHER_T2U_PLUS  = "Archer T2U+"     # https://wikidevi.wi-cat.ru/TP-LINK_Archer_T2U_Plus
    ARCHER_T2UHP     = "Archer T2UHP"    # https://wikidevi.wi-cat.ru/TP-LINK_Archer_T2UHP
    ARCHER_T2U_NANO  = "Archer T2U Nano" # https://wikidevi.wi-cat.ru/TP-LINK_Archer_T2U_Nano
    ARCHER_T3U       = "Archer T3U"      # https://wikidevi.wi-cat.ru/TP-LINK_Archer_T3U
    ARCHER_T3U_NANO  = "Archer T3U Nano" # https://wikidevi.wi-cat.ru/TP-LINK_Archer_T3U_Nano
    ARCHER_T3U_PLUS  = "Archer T3U Plus" # https://wikidevi.wi-cat.ru/TP-LINK_Archer_T3U_Plus
    ARCHER_T4U       = "Archer T4U"      # https://wikidevi.wi-cat.ru/TP-LINK_Archer_T4U
    ARCHER_T4UH      = "Archer T4UH"     # https://wikidevi.wi-cat.ru/TP-LINK_Archer_T4UH
    ARCHER_T4UH_V2   = "Archer T4UH v2"  # https://wikidevi.wi-cat.ru/TP-LINK_Archer_T4UH
    ARCHER_T4UHP     = "Archer T4UHP"    # https://wikidevi.wi-cat.ru/TP-LINK_Archer_T4UHP
    ARCHER_T4UHP_V2  = "Archer T4UHP v2" # https://linux-hardware.org/?id=usb:2357-0122
    ARCHER_T4U_PLUS  = "Archer T4U Plus" # https://wikidevi.wi-cat.ru/TP-LINK_Archer_T4U_Plus
    ARCHER_T4U_V2    = "Archer T4U v2"   # https://wikidevi.wi-cat.ru/TP-LINK_Archer_T4U_v2
    ARCHER_T4U_V3    = "Archer T4U V3"   # https://wikidevi.wi-cat.ru/TP-LINK_Archer_T4U_v3
    ARCHER_T9UH      = "Archer T9UH"     # https://wikidevi.wi-cat.ru/TP-LINK_Archer_T9UH
    ARCHER_TX20U_PLUS = "Archer TX20U+"  # https://wikidevi.wi-cat.ru/TP-LINK_Archer_TX35U_Plus (diff page) says: TP-LINK Archer TX20U Plus (AX1800) • RTL8832AU [2357:013f]
    TL_WDN6200       = "TL-WDN6200"      # https://github.com/aircrack-ng/rtl8812au/issues/1262
    TL_WN322G_V2_V3  = "TL-WN322G v2/v3" # https://wikidevi.wi-cat.ru/TP-LINK_TL-WN322G_v3
    TL_WN722N_V1     = "TL-WN722N v1"    # https://wikidevi.wi-cat.ru/TP-LINK_TL-WN722N_v1.x
    TL_WN722N_V2_V3  = "TL-WN722N v2/v3" # https://wikidevi.wi-cat.ru/TP-LINK_TL-WN722N_v2 & v3
    TL_WN723N_V2_3_4 = "TL-WN723N v2/3/4" # https://wikidevi.wi-cat.ru/TP-LINK_TL-WN723N_v3


class TRENDnet(ProductName):
    TEW_805UB  = "TEW-805UB"
    TEW_805UBH = "TEW-805UBH"
    TEW_806UBH = "TEW-806UBH"
    TEW_808UBM = "TEW-808UBM"
    TEW_809UB  = "TEW-809UB"


class Turbolink(ProductName):
    UB801RE = "UB801RE"


class Ubiquiti(ProductName):
    WIFISTATION     = "WiFiStation"
    WIFISTATION_EXT = "WiFiStation EXT"


class VIA(ProductName):
    _802_11BGN = "VIA 802.11bgn"


class VTech(ProductName):
    RT2570 = "VTech RT2570"


class WD(ProductName):
    MYNET = "WD MyNet"


class WistronNeWeb(ProductName):
    DAUK_W8812 = "DAUK-W8812"


class Zinwell(ProductName):
    ZWX_G261 = "Zinwell ZWX-G261"


class Zyxel(ProductName):
    NWD6505 = "Zyxel NWD6505"
    NWD6605 = "ZyXEL NWD6605"


# OUI-ambiguous combined labels: one VID:PID, two makes; the driver's OUI refiner
# narrows to one post-connect (see each chip's derive_product_name).
AMBIGUOUS_AR9271   = " / ".join([ALFA.AWUS036NHA, TPLink.TL_WN722N_V1])
AMBIGUOUS_MT7612U  = " / ".join([ALFA.AWUS036ACM, Aukey.USB_AC1200])
AMBIGUOUS_MT7921AU = " / ".join([ALFA.AWUS036AXML, Panda.PAU0F])
