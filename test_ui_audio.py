import sys
import time
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer
from cema_app import CEMAApp

def run_test():
    app = QApplication(sys.argv)
    window = CEMAApp()
    
    # Simulate user turning to 95.9 and clicking "FORCE DEMOD"
    print("Simulating UI interaction...")
    window.freq_input.setValue(95.9)
    window.launch_demodulator(95.9)
    
    # Wait for 5 seconds to let the audio thread run
    QTimer.singleShot(5000, app.quit)
    app.exec()
    
if __name__ == "__main__":
    run_test()
