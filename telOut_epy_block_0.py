import numpy as np
from gnuradio import gr

class blk(gr.sync_block):
    def __init__(self, num_channels=32, threshold=0.01):
        gr.sync_block.__init__(
            self,
            name='FHSS Channel Auto-Selector',
            in_sig=[np.complex64] * num_channels,
            out_sig=[np.complex64]
        )
        self.num_channels = num_channels
        self.threshold = threshold
        self.active_ch = 0

    def work(self, input_items, output_items):
        # Calculate mean power across current buffer for each channel
        powers = [np.mean(np.abs(input_items[ch])**2) for ch in range(self.num_channels)]
        
        peak_ch = int(np.argmax(powers))
        if powers[peak_ch] > self.threshold:
            self.active_ch = peak_ch

        # Pass active channel's complex IQ stream straight to output
        output_items[0][:] = input_items[self.active_ch][:]
        return len(output_items[0])