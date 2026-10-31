import csv
import time
import threading
import queue
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox

import serial
from serial.tools import list_ports


# ---------------- CONFIGURATION ----------------

BAUD_RATE = 115200
CHANNELS = [
    "F1_405", "F2_425", "FZ_450", "F3_475",
    "F4_515", "F5_550", "FY_555", "FXL_600",
    "F6_640", "F7_690", "F8_745", "NIR_855"
]

DATA_DIR = Path(__file__).resolve().parent / "banana_spectral_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
MASTER_CSV = DATA_DIR / "banana_spectral_dataset.csv"

METADATA_COLUMNS = [
    "timestamp", "date", "elapsed_seconds",
    "banana_id", "session_id", "sample_no", "capture_mode"
]
CSV_COLUMNS = METADATA_COLUMNS + CHANNELS

DURATION_OPTIONS = {
    "5 minutes": 5 * 60,
    "10 minutes": 10 * 60,
    "20 minutes": 20 * 60,
    "30 minutes": 30 * 60,
    "1 hour": 60 * 60,
    "2 hours": 2 * 60 * 60,
    "Until stopped": None
}

# ---------------- GLOBAL STATE ----------------

ser = None
connected = False
session_running = False
stop_event = threading.Event()
messages = queue.Queue()
capture_lock = threading.Lock()


# ---------------- CSV FUNCTIONS ----------------

def create_session_csv(banana_id, mode):
    """Create a separate CSV file for this capture session."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = DATA_DIR / f"{banana_id}_{mode}_{timestamp}.csv"
    with open(path, "w", newline="", encoding="utf-8") as file:
        csv.DictWriter(file, fieldnames=CSV_COLUMNS).writeheader()
    return path


def save_reading(session_file, banana_id, session_id,
                 sample_no, mode, start_time, readings):
    """Save a row to the session CSV and the combined master CSV."""
    now = datetime.now()
    row = {
        "timestamp": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "elapsed_seconds": round(time.monotonic() - start_time, 2),
        "banana_id": banana_id,
        "session_id": session_id,
        "sample_no": sample_no,
        "capture_mode": mode,
    }
    row.update(dict(zip(CHANNELS, readings)))

    # Each file is opened and closed for every saved reading.
    with open(session_file, "a", newline="", encoding="utf-8") as file:
        csv.DictWriter(file, fieldnames=CSV_COLUMNS).writerow(row)

    master_exists = MASTER_CSV.exists() and MASTER_CSV.stat().st_size > 0
    with open(MASTER_CSV, "a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
        if not master_exists:
            writer.writeheader()
        writer.writerow(row)


# ---------------- SENSOR COMMUNICATION ----------------

def read_sensor():
    """Request one reading from Arduino and return 12 integer channels."""
    if ser is None or not ser.is_open:
        raise RuntimeError("Arduino is not connected.")

    # Drain stale serial output before requesting a new reading.
    ser.reset_input_buffer()
    ser.write(b"R")
    ser.flush()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        line = ser.readline().decode("utf-8", errors="ignore").strip()
        if not line:
            continue
        if line.startswith("ERROR"):
            raise RuntimeError(line)

        values = line.split(",")
        if len(values) == len(CHANNELS):
            try:
                return [int(value.strip()) for value in values]
            except ValueError:
                continue

    raise TimeoutError("No valid 12-channel reading received within 10 seconds.")


# ---------------- CAPTURE WORKER ----------------

def capture_worker(mode, banana_id, sample_count,
                   interval_value, duration_choice):
    global session_running

    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    start_time = time.monotonic()
    duration_seconds = DURATION_OPTIONS.get(duration_choice)
    sample_no = 0
    session_file = create_session_csv(banana_id, mode)

    messages.put(("file", str(session_file)))
    messages.put(("status", f"Capturing {banana_id}: {mode} mode"))

    try:
        while not stop_event.is_set():
            elapsed = time.monotonic() - start_time

            # For timed continuous captures, do not start another reading
            # once the selected duration has elapsed.
            if (mode == "continuous"
                    and duration_seconds is not None
                    and elapsed >= duration_seconds):
                break

            try:
                readings = read_sensor()
            except Exception as error:
                messages.put(("error", str(error)))
                break

            # If Stop was pressed while the sensor was reading, save the
            # completed reading before exiting.
            sample_no += 1
            try:
                save_reading(
                    session_file, banana_id, session_id,
                    sample_no, mode, start_time, readings
                )
            except Exception as error:
                messages.put(("error", f"CSV save failed: {error}"))
                break

            messages.put((
                "progress", sample_no, readings,
                time.monotonic() - start_time
            ))

            if stop_event.is_set():
                break

            if mode == "single":
                break

            if mode == "batch" and sample_no >= sample_count:
                break

            # Batch interval is in seconds; continuous interval is in minutes.
            wait_time = (
                interval_value if mode == "batch"
                else interval_value * 60
            )

            if mode == "continuous" and duration_seconds is not None:
                remaining = duration_seconds - (time.monotonic() - start_time)
                if remaining <= 0:
                    break
                wait_time = min(wait_time, remaining)

            if stop_event.wait(wait_time):
                break

    finally:
        # Session/master files are closed after each row is written.
        messages.put(("finished", mode, sample_no, str(session_file)))


# ---------------- GUI ACTIONS ----------------

def refresh_ports():
    ports = [port.device for port in list_ports.comports()]
    port_box["values"] = ports
    if ports and port_var.get() not in ports:
        port_var.set(ports[0])


def connect_sensor():
    global ser, connected

    if session_running:
        return

    port = port_var.get().strip()
    if not port:
        messagebox.showerror("Port Error", "Select the Arduino COM port.")
        return

    try:
        # Close a previous serial handle if one exists.
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

        ser = serial.Serial(port, BAUD_RATE, timeout=1)
        time.sleep(2)  # Arduino Uno may reset when serial opens.
        ser.reset_input_buffer()

        deadline = time.monotonic() + 15
        ready = False
        while time.monotonic() < deadline:
            line = ser.readline().decode("utf-8", errors="ignore").strip()
            if line == "READY":
                ready = True
                break

        if not ready:
            ser.close()
            ser = None
            raise RuntimeError(
                "Arduino did not send READY. Check the sketch and connection."
            )

        # Arduino sketch prints READY followed by the CSV header.
        header = ser.readline().decode("utf-8", errors="ignore").strip()
        if header.split(",") != CHANNELS:
            ser.close()
            ser = None
            raise RuntimeError(
                "Channel header mismatch. Upload the matching Arduino sketch."
            )

        connected = True
        status_var.set(f"Connected to {port}")
        connect_button.config(state="disabled")
        disconnect_button.config(state="normal")
        update_capture_buttons()

    except Exception as error:
        connected = False
        status_var.set("Not connected")
        messagebox.showerror("Connection Error", str(error))


def disconnect_sensor():
    global ser, connected

    if session_running:
        messagebox.showwarning(
            "Capture Running",
            "Stop capture and wait for it to finish before disconnecting."
        )
        return

    if ser is not None:
        try:
            ser.close()
        except Exception:
            pass

    ser = None
    connected = False
    status_var.set("Disconnected")
    connect_button.config(state="normal")
    disconnect_button.config(state="disabled")
    update_capture_buttons()


def update_capture_buttons():
    state = "normal" if connected and not session_running else "disabled"
    single_button.config(state=state)
    batch_button.config(state=state)
    continuous_button.config(state=state)
    stop_button.config(state="normal" if session_running else "disabled")
    disconnect_button.config(
        state="normal" if connected and not session_running else "disabled"
    )


def start_capture(mode):
    global session_running

    if not connected or ser is None or not ser.is_open:
        messagebox.showerror("Not Connected", "Connect the Arduino first.")
        return

    if session_running:
        return

    banana_id = banana_var.get()
    # Batch sampling uses seconds; continuous sampling uses minutes.
    interval_value = int(
        batch_interval_var.get() if mode == "batch"
        else continuous_interval_var.get()
    )
    sample_count = int(sample_count_var.get()) if mode == "batch" else 1
    duration_choice = duration_var.get()

    if mode == "continuous" and duration_choice not in DURATION_OPTIONS:
        messagebox.showerror("Duration Error", "Select a capture duration.")
        return

    stop_event.clear()
    session_running = True
    progress_var.set("Starting capture...")
    latest_var.set("Latest readings: —")
    status_var.set(f"Starting {mode} capture for {banana_id}")
    update_capture_buttons()

    worker = threading.Thread(
        target=capture_worker,
        args=(
            mode, banana_id, sample_count,
            interval_value, duration_choice
        ),
        daemon=True
    )
    worker.start()


def stop_capture():
    if session_running:
        stop_event.set()
        status_var.set("Stopping after the current sensor reading...")


def process_messages():
    global session_running

    try:
        while True:
            item = messages.get_nowait()
            kind = item[0]

            if kind == "status":
                status_var.set(item[1])

            elif kind == "file":
                current_file_var.set(item[1])

            elif kind == "progress":
                _, sample_no, readings, elapsed = item
                progress_var.set(
                    f"Samples saved: {sample_no} | "
                    f"Elapsed: {elapsed / 60:.2f} minutes"
                )
                latest_var.set(
                    "Latest readings: " + ", ".join(
                        f"{name}={value}"
                        for name, value in zip(CHANNELS, readings)
                    )
                )

            elif kind == "error":
                status_var.set("Capture error")
                messagebox.showerror("Capture Error", item[1])

            elif kind == "finished":
                _, mode, sample_no, filename = item
                session_running = False
                update_capture_buttons()
                status_var.set(f"{mode.capitalize()} capture finished.")
                progress_var.set(f"Finished: {sample_no} sample(s) saved.")
                current_file_var.set(filename)

    except queue.Empty:
        pass

    root.after(200, process_messages)


def close_app():
    if session_running:
        if messagebox.askyesno(
            "Capture Running",
            "Stop capture and close the application?"
        ):
            stop_event.set()
            root.after(300, close_app)
        return

    if ser is not None:
        try:
            ser.close()
        except Exception:
            pass

    root.destroy()


# ---------------- GUI LAYOUT ----------------

root = tk.Tk()
root.title("AS7343 Banana Shelf-Life Data Collector")
root.geometry("900x650")
root.minsize(760, 560)

port_var = tk.StringVar()
banana_var = tk.StringVar(value="B01")
sample_count_var = tk.StringVar(value="10")
batch_interval_var = tk.StringVar(value="15")
continuous_interval_var = tk.StringVar(value="20")
duration_var = tk.StringVar(value="Until stopped")

status_var = tk.StringVar(value="Not connected")
progress_var = tk.StringVar(value="No capture started")
current_file_var = tk.StringVar(value="No session file yet")
latest_var = tk.StringVar(value="Latest readings: —")

main = ttk.Frame(root, padding=15)
main.pack(fill="both", expand=True)

ttk.Label(
    main,
    text="AS7343 Banana Spectral Data Collector",
    font=("Arial", 16, "bold")
).pack(anchor="w", pady=(0, 15))

# Connection controls
connection_frame = ttk.LabelFrame(
    main, text="Arduino Connection", padding=10
)
connection_frame.pack(fill="x", pady=5)

ttk.Label(connection_frame, text="COM Port:").grid(
    row=0, column=0, padx=5, pady=5
)
port_box = ttk.Combobox(
    connection_frame, textvariable=port_var,
    width=15, state="readonly"
)
port_box.grid(row=0, column=1, padx=5, pady=5)

ttk.Button(
    connection_frame, text="Refresh Ports", command=refresh_ports
).grid(row=0, column=2, padx=5, pady=5)

connect_button = ttk.Button(
    connection_frame, text="Connect", command=connect_sensor
)
connect_button.grid(row=0, column=3, padx=5, pady=5)

disconnect_button = ttk.Button(
    connection_frame, text="Disconnect",
    command=disconnect_sensor, state="disabled"
)
disconnect_button.grid(row=0, column=4, padx=5, pady=5)

# Sampling settings
settings_frame = ttk.LabelFrame(
    main, text="Sampling Settings", padding=10
)
settings_frame.pack(fill="x", pady=5)

ttk.Label(settings_frame, text="Select Banana:").grid(
    row=0, column=0, padx=5, pady=8, sticky="w"
)
ttk.Combobox(
    settings_frame, textvariable=banana_var,
    values=["B01", "B02", "B03", "B04", "B05", "B06"],
    state="readonly", width=14
).grid(row=0, column=1, padx=5, pady=8)

ttk.Label(settings_frame, text="Samples per batch:").grid(
    row=0, column=2, padx=5, pady=8, sticky="w"
)
ttk.Combobox(
    settings_frame, textvariable=sample_count_var,
    values=["5", "10", "15", "20", "30", "50"],
    state="readonly", width=12
).grid(row=0, column=3, padx=5, pady=8)

ttk.Label(settings_frame, text="Batch Interval:").grid(
    row=1, column=0, padx=5, pady=8, sticky="w"
)
ttk.Combobox(
    settings_frame, textvariable=batch_interval_var,
    values=["1", "5", "10", "15", "20", "30", "45", "60"],
    state="readonly", width=14
).grid(row=1, column=1, padx=5, pady=8)

ttk.Label(settings_frame, text="Batch interval unit: seconds").grid(
    row=1, column=2, columnspan=2, padx=5, pady=8, sticky="w"
)

ttk.Label(settings_frame, text="Continuous Interval:").grid(
    row=2, column=0, padx=5, pady=8, sticky="w"
)
ttk.Combobox(
    settings_frame, textvariable=continuous_interval_var,
    values=["1", "5", "10", "15", "20", "30", "45", "60"],
    state="readonly", width=14
).grid(row=2, column=1, padx=5, pady=8)

ttk.Label(settings_frame, text="Continuous interval unit: minutes").grid(
    row=2, column=2, columnspan=2, padx=5, pady=8, sticky="w"
)

ttk.Label(settings_frame, text="Continuous Duration:").grid(
    row=3, column=0, padx=5, pady=8, sticky="w"
)
ttk.Combobox(
    settings_frame, textvariable=duration_var,
    values=list(DURATION_OPTIONS.keys()),
    state="readonly", width=14
).grid(row=3, column=1, padx=5, pady=8)

ttk.Label(
    settings_frame,
    text="Duration controls when continuous capture auto-stops"
).grid(row=3, column=2, columnspan=2, padx=5, pady=8, sticky="w")

# Capture controls
capture_frame = ttk.LabelFrame(
    main, text="Capture Controls", padding=10
)
capture_frame.pack(fill="x", pady=5)

single_button = ttk.Button(
    capture_frame, text="Single Capture",
    command=lambda: start_capture("single"), state="disabled"
)
single_button.grid(row=0, column=0, padx=5, pady=8)

batch_button = ttk.Button(
    capture_frame, text="Start Batch Capture",
    command=lambda: start_capture("batch"), state="disabled"
)
batch_button.grid(row=0, column=1, padx=5, pady=8)

continuous_button = ttk.Button(
    capture_frame, text="Start Continuous Capture",
    command=lambda: start_capture("continuous"), state="disabled"
)
continuous_button.grid(row=0, column=2, padx=5, pady=8)

stop_button = ttk.Button(
    capture_frame, text="Stop Capture",
    command=stop_capture, state="disabled"
)
stop_button.grid(row=0, column=3, padx=5, pady=8)

# Status area
status_frame = ttk.LabelFrame(
    main, text="Capture Status", padding=10
)
status_frame.pack(fill="both", expand=True, pady=5)

ttk.Label(
    status_frame, textvariable=status_var, wraplength=820
).pack(anchor="w", pady=4)

ttk.Label(
    status_frame, textvariable=progress_var, wraplength=820
).pack(anchor="w", pady=4)

ttk.Label(status_frame, text="Current session CSV:").pack(
    anchor="w", pady=(8, 2)
)
ttk.Label(
    status_frame, textvariable=current_file_var, wraplength=820
).pack(anchor="w", pady=2)

ttk.Label(
    status_frame, textvariable=latest_var, wraplength=820
).pack(anchor="w", pady=8)

ttk.Label(
    main, text=f"Data folder: {DATA_DIR}", wraplength=850
).pack(anchor="w", pady=5)

refresh_ports()
root.after(200, process_messages)
root.protocol("WM_DELETE_WINDOW", close_app)
root.mainloop()
