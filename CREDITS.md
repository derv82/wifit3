# Credits — the shoulders we stand on

wifit3's userland drivers are clean-room Python re-implementations of **GPLv2 Linux kernel
and vendor DKMS drivers**. Every card wifit3 supports works because someone — often over
fifteen-plus years — first reverse-engineered the silicon and wrote, debugged, and
maintained the driver we ported from. This file credits them.

**How this list was built.** We tallied commit authorship of each upstream driver — the
mainline `torvalds/linux` driver paths and the vendor GitHub repos — and mapped every
substantive contributor to the wifit3 card(s) their work made possible. Tree-wide
mechanical commits (checkpatch / SPDX / build-warning sweeps) are filtered out so the
people who actually *built* the drivers stand out. Ordering favors **breadth** (how many of
our drivers a person underpins) then **depth** (volume of authorship upstream) — though the vendor list opens with our deepest personal thanks (below), metrics aside. A `—` means
the kernel commits carry no linked GitHub account (common for vendor `@realtek.com` authors
and pre-GitHub-era contributors).

> **Maintainers:** when a new chipset is ported, update this file. See
> `docs/porting/METHODOLOGY.md` → "Housekeeping".

---

## Vendor / DKMS maintainers

The out-of-tree vendor drivers we ported the `*_dkms` variants from — the people who kept
Realtek USB Wi-Fi working for the Linux community, year after year, outside the kernel tree.

- **Christian "kimo" B.** ([@kimocoder](https://github.com/kimocoder)) — RTL8188EUS.
  **Our biggest thanks.** Christian took over **wifite2** when its original maintainer
  stepped away, and has kept it alive and evolving for years since — wifit3 owes its
  lineage to that work. (He also maintains `aircrack-ng`'s RTL8188EUS driver, ported here.)
- **Nick Morrow** ([@morrownr](https://github.com/morrownr)) — RTL8812AU, RTL8821AU, RTL8814AU, RTL8822BU, MT7921AU
- **@5kft** ([@5kft](https://github.com/5kft)) — RTL8821AU, RTL8822BU
- **jose guzman** ([@joseguzman1337](https://github.com/joseguzman1337)) — RTL8814AU
- **Andras Gemes** ([@gemesa](https://github.com/gemesa)) — RTL8821AU
- **@gglluukk** ([@gglluukk](https://github.com/gglluukk)) — RTL8188EUS
- **@misha4gps** ([@misha4gps](https://github.com/misha4gps)) — RTL8822BU
- **Victor Golovanenko** ([@drygdryg](https://github.com/drygdryg)) — RTL8188EUS
- **Stephen Oliver** ([@steveatinfincia](https://github.com/steveatinfincia)) — RTL8188EUS
- **Igor Pečovnik** ([@igorpecovnik](https://github.com/igorpecovnik)) — RTL8188EUS
- **Joseph LaFreniere** ([@lafrenierejm](https://github.com/lafrenierejm)) — RTL8812AU
- **Hamdan** ([@SmartBoy84](https://github.com/SmartBoy84)) — RTL8188EUS
- **LIChengGang** ([@Zeno-sole](https://github.com/Zeno-sole)) — RTL8812AU, RTL8821AU, RTL8814AU, RTL8822BU

---

## Mainline (Linux kernel) authors & maintainers

The people who wrote and maintained these drivers in `drivers/net/wireless/` — across the
Ralink (`rt2x00`), MediaTek (`mt76`), Atheros (`ath9k`), and Realtek (`rtl818x`, `rtw88`)
trees.

- **Stanislaw Gruszka** ([@sgruszka](https://github.com/sgruszka)) — RT2500USB, RT2800USB, RT3070, RT5370, RT5372, RT5572, MT7610U, MT7612U, MT7921AU, AR9271
- **Lorenzo Bianconi** ([@LorenzoBianconi](https://github.com/LorenzoBianconi)) — MT7610U, MT7612U, MT7921AU, AR9271
- **Felix Fietkau** ([@nbd168](https://github.com/nbd168)) — MT7610U, MT7612U, MT7921AU, AR9271
- **Ivo van Doorn** (—) — RT2500USB, RT2800USB, RT3070, RT5370, RT5372, RT5572
- **Gertjan van Wingerde** (—) — RT2500USB, RT2800USB, RT3070, RT5370, RT5372, RT5572
- **Helmut Schaa** (—) — RT2500USB, RT2800USB, RT3070, RT5370, RT5372, RT5572
- **Sujith Manoharan** (—) — AR9271
- **Bitterblue Smith** (—) — RTL8812AU, RTL8821AU, RTL8814AU, RTL8822BU
- **Sean Wang** ([@moore-bros](https://github.com/moore-bros)) — MT7610U, MT7612U, MT7921AU
- **Ryder Lee** ([@ryderlee1110](https://github.com/ryderlee1110)) — MT7610U, MT7612U, MT7921AU
- **Yan-Hsuan Chuang** (—) — RTL8812AU, RTL8821AU, RTL8814AU, RTL8822BU
- **Ping-Ke Shih** (—) — RTL8812AU, RTL8821AU, RTL8814AU, RTL8822BU
- **Gabor Juhos** ([@juhosg](https://github.com/juhosg)) — RT2800USB, RT3070, RT5370, RT5372, RT5572, AR9271
- **John W. Linville** ([@linvjw](https://github.com/linvjw)) — RT2500USB, RT2800USB, RT3070, RT5370, RT5372, RT5572, AR9271, RTL8187
- **Luis R. Rodriguez** ([@mcgrof](https://github.com/mcgrof)) — AR9271
- **Shayne Chen** ([@csyuanc](https://github.com/csyuanc)) — MT7610U, MT7612U, MT7921AU
- **Po-Hao Huang** (—) — RTL8812AU, RTL8821AU, RTL8814AU, RTL8822BU
- **Ching-Te Ku** ([@ku920601](https://github.com/ku920601)) — RTL8812AU, RTL8821AU, RTL8814AU, RTL8822BU
- **Deren Wu** ([@deren](https://github.com/deren)) — MT7610U, MT7612U, MT7921AU
- **Larry Finger** ([@lwfinger](https://github.com/lwfinger)) — RTL8187, RTL8822BU
- **Michael Wu** (—) — RTL8187
- **Bartlomiej Zolnierkiewicz** (—) — RT2800USB, RT3070, RT5370, RT5372, RT5572
- **Ming Yen Hsieh** (—) — MT7610U, MT7612U, MT7921AU
- **Zong-Zhe Yang** (—) — RTL8812AU, RTL8821AU, RTL8814AU, RTL8822BU
- **Chin-Yen Lee** (—) — RTL8812AU, RTL8821AU, RTL8814AU, RTL8822BU
- **Tzu-En Huang** (—) — RTL8812AU, RTL8821AU, RTL8814AU, RTL8822BU
- **Oleksij Rempel** ([@olerem](https://github.com/olerem)) — AR9271
- **Rajkumar Manoharan** (—) — AR9271
- **Peter Chiu** (—) — MT7610U, MT7612U, MT7921AU
- **Daniel Golle** ([@dangowrt](https://github.com/dangowrt)) — RT2800USB, RT3070, RT5370, RT5372, RT5572
- **Tomislav Požega** ([@psyborg55](https://github.com/psyborg55)) — RT2800USB, RT3070, RT5370, RT5372, RT5572
- **Xose Vazquez Perez** ([@xosevp](https://github.com/xosevp)) — RT2800USB, RT3070, RT5370, RT5372, RT5572
- **Andrey Skvortsov** ([@AndreySV](https://github.com/AndreySV)) — RTL8812AU, RTL8821AU, RTL8814AU, RTL8822BU
- **Chih-Kang Chang** (—) — RTL8812AU, RTL8821AU, RTL8814AU, RTL8822BU
- **Sascha Hauer** ([@saschahauer](https://github.com/saschahauer)) — RTL8812AU, RTL8821AU, RTL8814AU, RTL8822BU
- **Martin Blumenstingl** ([@xdarklight](https://github.com/xdarklight)) — RTL8822BU, AR9271
- **Howard Hsu** ([@haogroot](https://github.com/haogroot)) — MT7610U, MT7612U, MT7921AU
- **Quan Zhou** ([@Quanzhoucen](https://github.com/Quanzhoucen)) — MT7921AU
- **Leon Yen** ([@leon-yen](https://github.com/leon-yen)) — MT7921AU
- **Vasanthakumar Thiagarajan** (—) — AR9271
- **Mohammed Shafi Shajakhan** (—) — AR9271
- **Hin-Tak Leung** (—) — RTL8187
- **Herton Ronaldo Krzesinski** (—) — RTL8187
- **Dmitry Antipov** ([@dmantipov](https://github.com/dmantipov)) — RTL8812AU, RTL8821AU, RTL8814AU, RTL8822BU
- **Miaoqing Pan** ([@miaoqing-pan](https://github.com/miaoqing-pan)) — AR9271
- **Ben Greear** ([@greearb](https://github.com/greearb)) — MT7921AU, AR9271

### Foundational maintainers

The Linux wireless stack (`mac80211`/`cfg80211`) and subsystem stewardship every one of
these drivers is built on:

- **Johannes Berg** ([@jmberg-intel](https://github.com/jmberg-intel)) — `mac80211` / `cfg80211`
- **Kalle Valo** ([@kvalo](https://github.com/kvalo)) — Linux wireless maintainer

---

*This list is generated from public open-source commit history and is necessarily
incomplete — many more people sent fixes, reviews, and testing that the commit tally
doesn't capture. If you contributed to one of these drivers and we missed or miscredited
you, please open an issue or PR.*
