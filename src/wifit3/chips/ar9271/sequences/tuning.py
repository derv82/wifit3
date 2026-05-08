from ..constants import AR_PHY_SYNTH_CONTROL

# Synthesizer Words for Fractional-N Math (Channel -> Word)
SYNTH_WORDS = {
    1: 0x30a0cccc,
    6: 0x30a27777,
}

# The "Golden Sequence" - a simplified version for implementation.
# In a real scenario, this would be a large list of WMI_REG_WRITE payloads.
def get_channel_hop_sequence(channel: int):
    """
    Returns a list of (command_id, payload) tuples for a channel change.
    """
    seq = []
    
    # Placeholder: In a real implementation, this would contain the 50+ 
    # register writes captured in the golden templates.
    
    # The most critical poke: The Synthesizer Control
    synth_word = SYNTH_WORDS.get(channel, 0x30a27777)
    
    # 0x0015 = WMI_REG_WRITE_CMDID
    # Payload: [Addr(4, BE)] [Value(4, BE)]
    import struct
    synth_payload = struct.pack(">II", AR_PHY_SYNTH_CONTROL, synth_word)
    
    seq.append((0x0015, synth_payload))
    
    return seq
