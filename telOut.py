#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Not titled yet
# GNU Radio version: 3.10.12.0

from PyQt5 import Qt
from gnuradio import qtgui
from gnuradio import analog
from gnuradio import filter
from gnuradio.filter import firdes
from gnuradio import gr
from gnuradio.fft import window
import sys
import signal
from PyQt5 import Qt
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import eng_notation
from gnuradio import network
from gnuradio import soapy
from gnuradio.filter import pfb
from xmlrpc.server import SimpleXMLRPCServer
import threading
import telOut_epy_block_0 as epy_block_0  # embedded python block



class telOut(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "Not titled yet", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Not titled yet")
        qtgui.util.check_set_qss()
        try:
            self.setWindowIcon(Qt.QIcon.fromTheme('gnuradio-grc'))
        except BaseException as exc:
            print(f"Qt GUI: Could not set Icon: {str(exc)}", file=sys.stderr)
        self.top_scroll_layout = Qt.QVBoxLayout()
        self.setLayout(self.top_scroll_layout)
        self.top_scroll = Qt.QScrollArea()
        self.top_scroll.setFrameStyle(Qt.QFrame.NoFrame)
        self.top_scroll_layout.addWidget(self.top_scroll)
        self.top_scroll.setWidgetResizable(True)
        self.top_widget = Qt.QWidget()
        self.top_scroll.setWidget(self.top_widget)
        self.top_layout = Qt.QVBoxLayout(self.top_widget)
        self.top_grid_layout = Qt.QGridLayout()
        self.top_layout.addLayout(self.top_grid_layout)

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "telOut")

        try:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
        except BaseException as exc:
            print(f"Qt GUI: Could not restore geometry: {str(exc)}", file=sys.stderr)
        self.flowgraph_started = threading.Event()

        ##################################################
        # Variables
        ##################################################
        self.selected_channel = selected_channel = 0
        self.samp_rate = samp_rate = 20e6

        ##################################################
        # Blocks
        ##################################################

        self.xmlrpc_server_0 = SimpleXMLRPCServer(('localhost', 8080), allow_none=True)
        self.xmlrpc_server_0.register_instance(self)
        self.xmlrpc_server_0_thread = threading.Thread(target=self.xmlrpc_server_0.serve_forever)
        self.xmlrpc_server_0_thread.daemon = True
        self.xmlrpc_server_0_thread.start()
        self.soapy_hackrf_source_0 = None
        dev = 'driver=hackrf'
        stream_args = ''
        tune_args = ['']
        settings = ['']

        self.soapy_hackrf_source_0 = soapy.source(dev, "fc32", 1, '',
                                  stream_args, tune_args, settings)
        self.soapy_hackrf_source_0.set_sample_rate(0, samp_rate)
        self.soapy_hackrf_source_0.set_bandwidth(0, 0)
        self.soapy_hackrf_source_0.set_frequency(0, 915e6)
        self.soapy_hackrf_source_0.set_gain(0, 'AMP', True)
        self.soapy_hackrf_source_0.set_gain(0, 'LNA', min(max(16, 0.0), 40.0))
        self.soapy_hackrf_source_0.set_gain(0, 'VGA', min(max(20, 0.0), 62.0))
        self.rational_resampler_xxx_0 = filter.rational_resampler_ccc(
                interpolation=8,
                decimation=5,
                taps=[],
                fractional_bw=0)
        self.pfb_channelizer_ccf_0 = pfb.channelizer_ccf(
            32,
            firdes.low_pass(1.0, samp_rate, samp_rate/32/2, 100e3),
            1.0,
            100)
        self.pfb_channelizer_ccf_0.set_channel_map([])
        self.pfb_channelizer_ccf_0.declare_sample_delay(0)
        self.network_udp_sink_0_0 = network.udp_sink(gr.sizeof_gr_complex, 1, '127.0.0.1', 50051, 0, 1472, False)
        self.epy_block_0 = epy_block_0.blk(num_channels=32, threshold=0.001)
        self.dc_blocker_xx_0 = filter.dc_blocker_cc(32, True)
        self.analog_pwr_squelch_xx_0 = analog.pwr_squelch_cc((-35), 0.1, 0, True)


        ##################################################
        # Connections
        ##################################################
        self.connect((self.analog_pwr_squelch_xx_0, 0), (self.network_udp_sink_0_0, 0))
        self.connect((self.dc_blocker_xx_0, 0), (self.analog_pwr_squelch_xx_0, 0))
        self.connect((self.epy_block_0, 0), (self.rational_resampler_xxx_0, 0))
        self.connect((self.pfb_channelizer_ccf_0, 22), (self.epy_block_0, 22))
        self.connect((self.pfb_channelizer_ccf_0, 27), (self.epy_block_0, 27))
        self.connect((self.pfb_channelizer_ccf_0, 31), (self.epy_block_0, 31))
        self.connect((self.pfb_channelizer_ccf_0, 11), (self.epy_block_0, 11))
        self.connect((self.pfb_channelizer_ccf_0, 1), (self.epy_block_0, 1))
        self.connect((self.pfb_channelizer_ccf_0, 29), (self.epy_block_0, 29))
        self.connect((self.pfb_channelizer_ccf_0, 30), (self.epy_block_0, 30))
        self.connect((self.pfb_channelizer_ccf_0, 13), (self.epy_block_0, 13))
        self.connect((self.pfb_channelizer_ccf_0, 15), (self.epy_block_0, 15))
        self.connect((self.pfb_channelizer_ccf_0, 21), (self.epy_block_0, 21))
        self.connect((self.pfb_channelizer_ccf_0, 3), (self.epy_block_0, 3))
        self.connect((self.pfb_channelizer_ccf_0, 4), (self.epy_block_0, 4))
        self.connect((self.pfb_channelizer_ccf_0, 20), (self.epy_block_0, 20))
        self.connect((self.pfb_channelizer_ccf_0, 25), (self.epy_block_0, 25))
        self.connect((self.pfb_channelizer_ccf_0, 26), (self.epy_block_0, 26))
        self.connect((self.pfb_channelizer_ccf_0, 23), (self.epy_block_0, 23))
        self.connect((self.pfb_channelizer_ccf_0, 24), (self.epy_block_0, 24))
        self.connect((self.pfb_channelizer_ccf_0, 2), (self.epy_block_0, 2))
        self.connect((self.pfb_channelizer_ccf_0, 10), (self.epy_block_0, 10))
        self.connect((self.pfb_channelizer_ccf_0, 8), (self.epy_block_0, 8))
        self.connect((self.pfb_channelizer_ccf_0, 19), (self.epy_block_0, 19))
        self.connect((self.pfb_channelizer_ccf_0, 17), (self.epy_block_0, 17))
        self.connect((self.pfb_channelizer_ccf_0, 18), (self.epy_block_0, 18))
        self.connect((self.pfb_channelizer_ccf_0, 16), (self.epy_block_0, 16))
        self.connect((self.pfb_channelizer_ccf_0, 12), (self.epy_block_0, 12))
        self.connect((self.pfb_channelizer_ccf_0, 0), (self.epy_block_0, 0))
        self.connect((self.pfb_channelizer_ccf_0, 5), (self.epy_block_0, 5))
        self.connect((self.pfb_channelizer_ccf_0, 14), (self.epy_block_0, 14))
        self.connect((self.pfb_channelizer_ccf_0, 28), (self.epy_block_0, 28))
        self.connect((self.pfb_channelizer_ccf_0, 9), (self.epy_block_0, 9))
        self.connect((self.pfb_channelizer_ccf_0, 6), (self.epy_block_0, 6))
        self.connect((self.pfb_channelizer_ccf_0, 7), (self.epy_block_0, 7))
        self.connect((self.rational_resampler_xxx_0, 0), (self.dc_blocker_xx_0, 0))
        self.connect((self.soapy_hackrf_source_0, 0), (self.pfb_channelizer_ccf_0, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "telOut")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_selected_channel(self):
        return self.selected_channel

    def set_selected_channel(self, selected_channel):
        self.selected_channel = selected_channel

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.pfb_channelizer_ccf_0.set_taps(firdes.low_pass(1.0, self.samp_rate, self.samp_rate/32/2, 100e3))
        self.soapy_hackrf_source_0.set_sample_rate(0, self.samp_rate)




def main(top_block_cls=telOut, options=None):

    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls()

    tb.start()
    tb.flowgraph_started.set()

    tb.show()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()

        Qt.QApplication.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    timer = Qt.QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    qapp.exec_()

if __name__ == '__main__':
    main()
