To detect WPA3 and SAE natively in Wifit3, you should prioritize updating your beacon and probe response parsing logic to extract specific bits from the **RSN (Robust Security Network) Information Element**.

### Prioritized Changes for Wifit3 802.11 Parsing Logic

#### 1. RSN Information Element (IE) Identification

* **Locate Tag 48 (0x30):** This is the RSN Information Element.
* **Identify AKM Suites:** Navigate to the Authentication and Key Management (AKM) suite list within the IE.
* **WPA2 vs. WPA3:**
* **WPA2-PSK:** Look for AKM Suite Type `2` (OUI `00:0F:AC:02`).
* **WPA3-SAE:** Look for AKM Suite Type `8` (OUI `00:0F:AC:08`).
* **Transition Mode:** If both Suite Type `2` and Suite Type `8` are present in the same beacon, the network is in Transition Mode.



#### 2. PMF (Protected Management Frames) Status

You must parse the **RSN Capabilities** field (a 2-byte field near the end of the RSN IE) to determine if management frames are protected. This dictates whether deauthentication attacks are viable.

* **MFPC (Bit 7):** Management Frame Protection Capable.
* **MFPR (Bit 8):** Management Frame Protection Required.
* **Attack Decision Logic:**
* **MFPR = 1:** PMF is **Required**. The AP will ignore unauthenticated deauthentication frames. You must use passive capture or PMKID attacks.
* **MFPC = 1, MFPR = 0:** PMF is **Optional**. Deauthentication may still work on some clients.
* **MFPC = 0:** PMF is **Disabled**. Standard deauthentication attacks are fully viable.



#### 3. SAE Handshake Validation (The "Hash" Capture)

Since you are bypassing external tools, your capture engine needs to recognize the **Authentication Type 11 (SAE)** exchange.

* **Identify SAE Frames:** Look for Management frames where the Authentication Algorithm is set to `3`.
* **Handshake Integrity:** To consider a "handshake" captured for WPA3, you need the **SAE Commit** and **SAE Confirm** frames, which contain the scalar and element data needed for cracking.

#### 4. SAE Group Identification (Optional but Recommended)

* Access points do not advertise supported SAE groups (curves) in beacons.
* **Wifit3 Logic:** When you capture a legitimate client's **SAE Commit** frame, parse the **Group Number** field (e.g., Group 19) to identify which curve is being used. This allows you to flag **Dragonblood-vulnerable** groups (22, 23, 24).

### Summary Table

| Feature | Field / Bit | Value for WPA3-SAE |
| --- | --- | --- |
| **IE Tag** | Element ID | 48 (0x30) |
| **AKM Suite** | Type | 8 (SAE) |
| **PMF Required** | RSN Cap Bit 8 | 1 |
| **PMF Capable** | RSN Cap Bit 7 | 1 |
| **Auth Algorithm** | Auth Fixed Field | 3 (SAE) |