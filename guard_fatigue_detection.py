import cv2
from scipy.spatial import distance
import mediapipe as mp
import numpy as np
import time
import sys
import subprocess
import argparse
import csv
import os

try:
    import sounddevice as sd  # Provided transitively by mediapipe, but optional
except Exception:  # pragma: no cover - optional; fall back on macOS system sound
    sd = None

class DrowsinessDetector:
    def __init__(self,
                 guard_mode: bool = False,
                 headless: bool = False,
                 fullscreen: bool = False,
                 log_path: str | None = None):
        self.LEFT_EYE = [362, 385, 387, 263, 373, 380]
        self.RIGHT_EYE = [33, 160, 158, 133, 153, 144]
        self.EAR_THRESH = 0.25
        self.CLOSED_EYES_FRAME = 20
        
        self.mp_face_mesh = mp.solutions.face_mesh
        self.face_mesh = self.mp_face_mesh.FaceMesh(
            max_num_faces=1,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5
        )
        
        self.counter = 0
        # Audio alert state
        self.alert_active = False
        self.last_beep_time = 0.0
        self.beep_interval_seconds = 1.0
        self.sample_rate = 44100
        self._prebuilt_beep = self._build_beep_waveform(
            frequency_hz=880.0, duration_seconds=0.25, volume=0.2
        )
        # Fatigue scoring
        self.fatigue_score = 0.0
        self.fatigue_increase_per_frame = 2.0
        self.fatigue_decay_per_frame = 1.0
        self.fatigue_alert_threshold = 60.0

        # Guard mode options
        self.guard_mode = guard_mode
        self.headless = headless
        self.fullscreen = fullscreen
        self.log_path = log_path

        # Guard mode state
        self.last_face_time = time.time()
        self.no_face_timeout_seconds = 10.0
        self.guard_alert_active = False
        self.guard_escalated = False
        self.escalation_seconds = 60.0
        # Fatigue scoring
        self.fatigue_score = 0.0
        self.fatigue_increase_per_frame = 2.0
        self.fatigue_decay_per_frame = 1.0
        self.fatigue_alert_threshold = 60.0
        
    def calculate_EAR(self, eye_points):
        A = distance.euclidean(eye_points[1], eye_points[5])
        B = distance.euclidean(eye_points[2], eye_points[4])
        C = distance.euclidean(eye_points[0], eye_points[3])
        ear = (A + B) / (2.0 * C)
        return ear
        
    def put_text_with_background(self, img, text, position, scale=1, color=(255,255,255), thickness=2):
        (text_width, text_height), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
        
        padding = 10
        bg_color = (0,0,255) if "ALERT" in text else (0,0,0)
        text_color = (255,255,255) if "ALERT" in text else color
        
        cv2.rectangle(img, 
                     (position[0] - padding, position[1] - text_height - padding),
                     (position[0] + text_width + padding, position[1] + padding),
                     bg_color, 
                     -1)
        
        cv2.putText(img, text, position, cv2.FONT_HERSHEY_SIMPLEX, scale, text_color, thickness)
    
    def process_frame(self, frame):
        frame = cv2.flip(frame, 1)
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = self.face_mesh.process(rgb_frame)
        
        if results.multi_face_landmarks:
            mesh_points = np.array([
                np.multiply([p.x, p.y], [frame.shape[1], frame.shape[0]]).astype(int)
                for p in results.multi_face_landmarks[0].landmark
            ])
            
            left_eye_points = mesh_points[self.LEFT_EYE]
            right_eye_points = mesh_points[self.RIGHT_EYE]
            
            left_ear = self.calculate_EAR(left_eye_points)
            right_ear = self.calculate_EAR(right_eye_points)
            
            avg_ear = (left_ear + right_ear) / 2.0
            
            cv2.polylines(frame, [left_eye_points], True, (0, 255, 0), 1)
            cv2.polylines(frame, [right_eye_points], True, (0, 255, 0), 1)

            # When a face is present, update face timestamp
            self.last_face_time = time.time()

            if avg_ear < self.EAR_THRESH:
                self.counter += 1
                # accumulate fatigue while likely drowsy
                self.fatigue_score = min(100.0, self.fatigue_score + self.fatigue_increase_per_frame)
                if self.counter >= self.CLOSED_EYES_FRAME or self.fatigue_score >= self.fatigue_alert_threshold:
                    self.put_text_with_background(frame, "Fatigue ALERT!", (10, 50), 
                                                scale=1.2, color=(0, 0, 255), thickness=2)
                    self._handle_audio_alert(active=True)
                    self._maybe_log_event("FATIGUE_ALERT")
            else:
                self.counter = 0
                # decay fatigue when attentive
                self.fatigue_score = max(0.0, self.fatigue_score - self.fatigue_decay_per_frame)
                self._handle_audio_alert(active=False)
                
            self.put_text_with_background(frame, 
                                        f"EAR (Eye Aspect Ratio): {avg_ear:.2f}", 
                                        (10, 100), 
                                        scale=0.7, 
                                        color=(255, 255, 255), 
                                        thickness=2)
            self.put_text_with_background(frame, 
                                        f"Fatigue score: {self.fatigue_score:.0f}", 
                                        (10, 140), 
                                        scale=0.7, 
                                        color=(255, 255, 255), 
                                        thickness=2)
        else:
            # No face detected: handle Guard Mode
            self._handle_no_face(frame)

        return frame

    def _build_beep_waveform(self, frequency_hz: float, duration_seconds: float, volume: float) -> np.ndarray:
        t = np.linspace(0, duration_seconds, int(self.sample_rate * duration_seconds), endpoint=False)
        waveform = (volume * np.sin(2 * np.pi * frequency_hz * t)).astype(np.float32)
        # Mono waveform; sounddevice accepts 1D float32
        return waveform

    def _play_beep(self) -> None:
        # Prefer sounddevice if available
        if sd is not None:
            try:
                sd.play(self._prebuilt_beep, self.sample_rate, blocking=False)
                return
            except Exception:
                pass
        # macOS fallback: system sound via afplay, non-blocking
        if sys.platform == 'darwin':
            try:
                subprocess.Popen([
                    'afplay', '/System/Library/Sounds/Ping.aiff'
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

    def _play_escalation_beep(self) -> None:
        # Louder/longer tone for escalation
        if sd is not None:
            try:
                tone = self._build_beep_waveform(1320.0, 0.5, 0.3)
                sd.play(tone, self.sample_rate, blocking=False)
                return
            except Exception:
                pass
        if sys.platform == 'darwin':
            try:
                subprocess.Popen([
                    'afplay', '/System/Library/Sounds/Sosumi.aiff'
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

    def _stop_audio(self) -> None:
        if sd is not None:
            try:
                sd.stop()
            except Exception:
                pass

    def _handle_audio_alert(self, active: bool) -> None:
        # Rising edge: became active now
        if active and not self.alert_active:
            self.alert_active = True
            self.last_beep_time = 0.0  # force immediate beep
        # Falling edge: stopped being active
        if not active and self.alert_active:
            self.alert_active = False
            self._stop_audio()
            return
        # While active, emit beep periodically
        if self.alert_active:
            now = time.time()
            if now - self.last_beep_time >= self.beep_interval_seconds:
                self._play_beep()
                self.last_beep_time = now
    
    def _handle_no_face(self, frame: np.ndarray) -> None:
        if not self.guard_mode:
            return
        now = time.time()
        seconds_since_face = now - self.last_face_time
        if seconds_since_face >= self.no_face_timeout_seconds:
            self.put_text_with_background(frame, "No Face ALERT!", (10, 50), scale=1.2, color=(0,0,255), thickness=2)
            self._handle_audio_alert(active=True)
            self._maybe_log_event("NO_FACE_ALERT")
            # Escalate after prolonged alert
            if seconds_since_face >= (self.no_face_timeout_seconds + self.escalation_seconds):
                if not self.guard_escalated:
                    self.guard_escalated = True
                    self._maybe_log_event("ALERT_ESCALATED")
                # Replace periodic beep with escalation beep occasionally
                if time.time() - self.last_beep_time >= max(0.5, self.beep_interval_seconds / 2):
                    self._play_escalation_beep()
                    self.last_beep_time = time.time()
        else:
            if self.alert_active or self.guard_alert_active:
                self._maybe_log_event("ALERT_CLEARED")
            self.guard_escalated = False
            self.guard_alert_active = False
            self._handle_audio_alert(active=False)

    def _maybe_log_event(self, event: str) -> None:
        if not self.log_path:
            return
        row = [time.strftime('%Y-%m-%d %H:%M:%S'), event]
        try:
            file_exists = os.path.exists(self.log_path)
            with open(self.log_path, 'a', newline='') as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(["timestamp", "event"]) 
                writer.writerow(row)
        except Exception:
            pass
    
    def start_detection(self):
        cap = cv2.VideoCapture(0)
        if not self.headless:
            cv2.namedWindow('Fatigue Detection', cv2.WINDOW_NORMAL)
            if self.fullscreen:
                try:
                    cv2.setWindowProperty('Fatigue Detection', cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
                except Exception:
                    pass
        
        if not cap.isOpened():
            raise Exception("Could not open camera")
            
        while True:
            ret, frame = cap.read()
            if not ret:
                break
                
            processed_frame = self.process_frame(frame)
            if not self.headless:
                cv2.imshow('Fatigue Detection', processed_frame)
            
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
                
        cap.release()
        cv2.destroyAllWindows()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fatigue detection with optional Guard Mode")
    parser.add_argument('--guard', action='store_true', help='Enable Guard Mode: alert on no-face timeout and escalate')
    parser.add_argument('--headless', action='store_true', help='Run without GUI window (audio alerts + logging only)')
    parser.add_argument('--fullscreen', action='store_true', help='Open window in fullscreen (if supported)')
    parser.add_argument('--log', type=str, default=None, help='CSV log file path for events')
    args = parser.parse_args()

    detector = DrowsinessDetector(
        guard_mode=args.guard,
        headless=args.headless,
        fullscreen=args.fullscreen,
        log_path=args.log
    )
    detector.start_detection()