"""Synthetic-sample CPU benchmark. Requires faster-whisper and a cached model."""
import argparse
import json
import time
import wave
import numpy as np
from faster_whisper import WhisperModel

parser = argparse.ArgumentParser()
parser.add_argument('audio')
parser.add_argument('--model', default='small')
args = parser.parse_args()
with wave.open(args.audio) as source:
    if source.getframerate() != 16000 or source.getnchannels() != 1: raise ValueError('Use 16 kHz mono WAV')
    audio = np.frombuffer(source.readframes(source.getnframes()), dtype='<i2').astype(np.float32) / 32768
start = time.perf_counter()
model = WhisperModel(args.model, device='cpu', compute_type='int8', cpu_threads=4, local_files_only=True)
print(json.dumps({'provider':'whisper-small-cpu-int8','modelLoadSeconds':round(time.perf_counter()-start,3)}), flush=True)
for beam in (1, 5):
    for trial in range(3):
        start = time.perf_counter()
        segments, _ = model.transcribe(audio, language='ru', beam_size=beam, vad_filter=False, condition_on_previous_text=False)
        text = ' '.join(s.text for s in segments)
        print(json.dumps({'provider':'whisper-small-cpu-int8','beam':beam,'trial':trial+1,
                          'recognitionSeconds':round(time.perf_counter()-start,3),'audioSeconds':round(len(audio)/16000,2),
                          'topicWordsRecognized':all(w in text.lower() for w in ('магазин','заказ','ответ'))}),flush=True)
