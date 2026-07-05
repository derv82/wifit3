## Full Circle (Story time)

In 2005 I wanted to get online and couldn't afford internet. I was working full time and going to
school, and some classes needed a connection the school computer lab didn't always have.

### Enter: Sandman

My friend Sandman came over with a laptop running Slackware, and after a few hours of guides, man
pages, and troubleshooting, we were on my neighbor's WEP network. The connection was spotty and I
never downloaded anything on it, just did schoolwork.

### Learning Linux

But I wanted to know *how* he did it, and why I couldn't do it on Windows. Sandman handed me a
Knoppix CD, which I got lost in and couldn't persist anything between the frequent
driver-kernel-panic reboots. From there it's a blur: Slackware dual-booted onto my shitty laptop,
then Backtrack. I learned Linux the hard way: GRUB, navigating the filesystem, KDE vs GNOME (was
GNOME even around then?).

### Learning Linux Drivers

Mostly I learned the pain of Linux wireless drivers. The only card I could afford was a Nintendo
Wi-Fi USB Connector (the Buffalo RT2570 in [the "Supported Hardware" table](README.md#supported-hardware)), 
bought so my Wii could reach the Wii Shop. (Okay, so I did do *some* non-schoolwork on the neighbor's wifi. Sue me.)
Its rt2500usb driver wasn't in whatever kernel I had, so I had to track it down and install it by hand.
Then *that* driver couldn't do monitor mode or injection, so I had to find a patch, compile it, and install *that*.
Weeks of effort and several bricked installs later, with a lot of patience from Sandman, I finally
had the card showing up in airmon/airodump.

### There Was No Spoon(WEP)

I cut my teeth on the aircrack suite by hand: memorizing the airodump and aireplay arguments,
running the WEP attack a step at a time, and walking my equally broke friends through it. A
big-red-button tool called SpoonWEP showed up later, the name being exactly what it sounds like: it
spoon-fed the whole attack to anyone who never learned the commands. It was the closest thing to
what Wifite would eventually become. But by then I'd already done it the slow way, which is the only
reason I understood it well enough to build my own.

### Grim WEPA is a terrible name

I was learning Java in school, so around Backtrack 4 I wrote **[GRIM WEPA](https://code.google.com/archive/p/grimwepa)**: an automated attack tool with a GUI that shelled out to air* processes in separate terminal windows.
It worked, sort of. It was buggy and honestly pretty bad, parsing stdout from airodump-ng.
And the name was even worse.

### Wifite (the first)

I wanted to learn Python and fix GRIM WEPA, so I wrote **[Wifite](https://github.com/derv82/wifite)** as I went: one ~3k-line file, completely unmaintainable, but it did the job, handled its edge cases, and sounded cool.
It caught on, got a mention in a [2011 New York Times piece](https://www.nytimes.com/2011/02/17/technology/personaltech/17basics.html),
and eventually shipped preinstalled in Kali. That one was a big deal for me.

### Wifite 2 (WiFi Harder)

Then I started at Amazon, actually learned to design software, and rewrote it as **[Wifite2](https://github.com/derv82/wifite2)**:
properly architected, extensible, with different ways to shell out to a process. I must have
gotten something right, because it's still in active use and is probably the most-used wifi
auditing tool around.

### Wifit3 with a Vengeance

Then I got laid off after 13 years, got rehired, and landed on a team that runs on LLMs. I started
wondering what I could build with them, and after weeks of back-and-forth with Gemini and Claude I
finally saw how to get Wifite onto Windows: the Minnie Drivers.

Gemini wrote a first MVP for the RTL8187L (the simplest card I could think of) by basically spraying
USB operations without understanding the bytes. It sort of worked. I saw beacons in a PowerShell window.
Then I asked it to port the AR9271 and it was a clusterfuck. I hit Gemini's ceiling, gave in, and paid for a
Claude subscription. Claude sorted out the AR9271 immediately, then ported the next card to beacons
in less than an hour. Repeat for every card over a few months, some TUI and UX improvements, and here we are.

And now I can crack a WEP network **on Windows** *with the Nintendo Wi-Fi USB Connector*. I've come
full circle. 20 years in the making. It's done.