import socket
import struct
import numpy as np
import queue
import threading

# Thread-safe queue to pass decoded data to your GUI
data_queue = queue.Queue()

def iq_receiver_thread():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 50051))
    sock.settimeout(1.0)

    print("Decoder bridge active: Listening for IQ stream...")

    try:
        while True:
            try:
                data, _ = sock.recvfrom(4096)
                if len(data) > 0:
                    # Unpack raw complex float32 IQ samples
                    iq_samples = np.frombuffer(data, dtype=np.complex64)
                    
                    # TODO: Insert your original decoder/demodulation logic here
                    # e.g., symbol slicing, correlation, or payload extraction
                    
                    # Example placeholder for processed packet data payload:
                    processed_payload = {
                        "samples_count": len(iq_samples),
                        "peak_mag": float(np.max(np.abs(iq_samples))),
                        "raw_bytes": data[:16].hex()
                    }
                    
                    # Push the mapped data to your GUI queue
                    data_queue.put(processed_payload)
                    
            except socket.timeout:
                pass
    except Exception as e:
        print(f"Receiver error: {e}")
    finally:
        sock.close()

# Start the background socket listener thread
listener_thread = threading.Thread(target=iq_receiver_thread, daemon=True)
listener_thread.start()

def get_latest_gui_data():
    """Call this from your GUI update loop to pull processed data without blocking."""
    items = []
    while not data_queue.empty():
        try:
            items.append(data_queue.get_nowait())
        except queue.Empty:
            break
    return items