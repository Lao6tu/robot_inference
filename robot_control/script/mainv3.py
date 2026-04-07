import io
import os
import socket
import struct
import time
import picamera2
import sys
import signal
import threading
import logging
from .server import Server
from .Buzzer import *

# Configuration log
logging.basicConfig(filename='/var/log/car_server.log', level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

class ServerController:
    def __init__(self):
        self.TCP_Server = Server()
        self.is_running = False
        self.threads = []
        self.stop_event = threading.Event()
        try:
            self.buzzer = Buzzer()  # Initialize Buzzer
            logging.info("Buzzer initialized successfully")
        except Exception as e:
            logging.error(f"Failed to initialize buzzer: {e}")
            raise

    def beep(self):
        try:
            logging.info("Playing startup sound")
            self.buzzer.run('1')  # Open Buzzer
            time.sleep(0.5)       # Wait 0.5s
            self.buzzer.run('0')  # Shutdown Buzzer
            logging.info("Startup sound completed")
        except Exception as e:
            logging.error(f"Buzzer error: {e}")

    def start_server(self):
        if not self.is_running:
            logging.info("Starting server...")
            try:
                # Using threads to perform buzzer operations
                beep_thread = threading.Thread(target=self.beep, daemon=True)
                beep_thread.start()
                
                self.TCP_Server.StartTcpServer()
                self.threads = [
                    threading.Thread(target=self.run_thread, args=(self.TCP_Server.readdata, "ReadData")),
                    threading.Thread(target=self.run_thread, args=(self.TCP_Server.sendvideo, "SendVideo")),
                    threading.Thread(target=self.run_thread, args=(self.TCP_Server.Power, "Power"))
                ]
                for thread in self.threads:
                    thread.daemon = True
                    thread.start()
                self.is_running = True
                logging.info("Server started successfully")
                
                # Waiting for the buzzer thread to finish
                beep_thread.join(timeout=2)  # Wait up to 2s
                if beep_thread.is_alive():
                    logging.warning("Beep thread did not complete in time")
                    
            except Exception as e:
                logging.error(f"Failed to start server: {e}")
                self.stop_server()
                raise
        else:
            logging.info("Server is already running")


    def run_thread(self, target, name):
        while not self.stop_event.is_set():
            try:
                target()
            except Exception as e:
                logging.error(f"Error in {name} thread: {e}")
                break

    def stop_server(self):
        if self.is_running:
            logging.info("Stopping server...")
            self.stop_event.set()
            self.TCP_Server.StopTcpServer()
            for thread in self.threads:
                thread.join(timeout=3)  # Give each thread 3s to finish
            self.is_running = False
            logging.info("Server stopped")
        else:
            logging.info("Server is not running")

    def run(self):
        self.start_server()  # Automatic opening of tcp server
        try:
            while not self.stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            logging.info("Program interrupted by user")
        finally:
            self.stop_server()

def cleanup():
    logging.info("Cleaning up resources...")
    try:
        if hasattr(picamera2.Picamera2, 'global_cleanup'):
            picamera2.Picamera2.global_cleanup()
        else:
            logging.warning("Picamera2 global_cleanup not available")
    except Exception as e:
        logging.error(f"Error during Picamera2 cleanup: {e}")

def handle_stop(signum, frame):
    logging.info("Stop signal received")
    controller.stop_server()

def handle_restart(signum, frame):
    logging.info("Restart signal received")
    controller.stop_server()
    controller.start_server()

if __name__ == '__main__':
    controller = ServerController()
    
    def shutdown(signum, frame):
        logging.info("Shutdown signal received")
        controller.stop_server()
        cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGUSR1, handle_stop)  # For stopping the server
    signal.signal(signal.SIGUSR2, handle_restart)  # For restarting the server

    try:
        controller.run()
    finally:
        cleanup()
        
        logging.info("Waiting for all threads to finish (10 seconds timeout)...")
        timeout = time.time() + 10
        while threading.active_count() > 1 and time.time() < timeout:
            time.sleep(0.1)
        
        remaining_threads = threading.enumerate()
        if len(remaining_threads) > 1:
            logging.warning(f"Force quitting. {len(remaining_threads) - 1} threads did not finish in time:")
            for thread in remaining_threads:
                if thread != threading.current_thread():
                    logging.warning(f"- {thread.name}")
            os._exit(1)
        else:
            logging.info("All threads finished. Exiting normally.")
            sys.exit(0)
