Yes, you can identify if a group is supported or rejected directly from the raw response frames. In an SAE exchange, the status information is contained in the **Authentication frame** (Management Type 0, Subtype 11).

### Identifying Support/Rejection from the Response

When you send an **SAE Commit** frame (Sequence 1) to an AP, the AP responds with its own Authentication frame. You can determine the outcome by looking at the **Status Code** field in that response:

1. **Rejection (Status 77):** If the AP does not support the group you proposed (e.g., Group 22), it will return **Status Code 77** (`0x004D`), which stands for **Authentication is rejected because the offered finite cyclic group is not supported**.
2. **Acceptance (Status 0):** If the group is supported, the AP returns **Status Code 0** (Successful) and includes its own SAE Commit scalar and element in the frame.
3. **Silent Drop:** Some APs may simply drop the frame if the group is invalid or unsupported, resulting in a timeout.

### How to "Mock" the Login in Wifit3

You can absolutely mock this behavior natively by implementing the first step of the SAE state machine. You don't need the full math for a simple probe; you just need to craft the **SAE Commit Request** frame.

#### 1. Frame Structure (Authentication Body)

After the standard 802.11 MAC header, the Authentication frame body for SAE consists of:

* **Authentication Algorithm (2 bytes):** Set to `3` (SAE).
* **Authentication Transaction Sequence (2 bytes):** Set to `1` (Commit).
* **Status Code (2 bytes):** Set to `0` for the request.
* **SAE Group (2 bytes):** The group number you are probing (e.g., 19, 20, 22).
* **Scalar & Element:** These are the "public key" components of the SAE exchange.

#### 2. The Native Implementation Strategy

Instead of calling `wpa_supplicant`, your Wifit3 logic would perform these steps:

* **Discovery:** Parse the beacon's **RSN Information Element** (Tag 48) to confirm the AP supports AKM Suite 8 (SAE).
* **Injection:** Construct a `bytearray` containing a valid SAE Commit frame for the group you want to test. (You can capture a "known good" Commit frame from a successful connection and simply swap the **Group ID** bytes).
* **Verification:** Listen for an Authentication frame (Subtype 11) from the BSSID with a Transaction Sequence of `1`.
* **Decision:** Read the two bytes at **offset 4** of the frame body (the Status Code).
* If `bytes[4:6] == \x4d\x00` (77), mark that group as **Rejected**.
* If `bytes[4:6] == \x00\x00` (0), mark that group as **Supported**.


This "move" allows Wifit3 to audit WPA3 group support (like identifying Dragonblood-vulnerable groups 22–24) without requiring external binaries or completing a full login.