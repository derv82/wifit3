
class TargetManager:
    """
    TODO: Implement using models.AccessPoint and models.Client
    - Store as Minified JSON? Pickles? Where do we store it? (`~/.wifit3` ?)
    Purpose of this class:

    engine/cache.py (TargetManager): This will be a central class that holds a dictionary of AccessPoint and Client models.
    - The Private Network (Hidden ESSID) Problem:
      - When the UI registers its RX callback with the WlanInterface, it routes the raw bytes into the TargetManager.
      - The manager parses the beacon. If it's a hidden network (<Hidden>), it caches the BSSID.
      - If it later sees a Probe Response or an Association Request from a client containing the real SSID for that BSSID,
        it instantly updates the cached AccessPoint model and notifies the UI.
    * Persistence: We can easily add a .save() and .load() method to this cache using Python's pickle or json to store known networks on disk between sessions.
    """

    def __init__(self):
        pass

    def save(self):
        pass

    def load(self):
        pass

    def get_target(self, bssid):
        # Construct if not already cached
        # return
        pass

    def get_access_point(self, bssid):
        # Construct if not already cached
        # return
        pass