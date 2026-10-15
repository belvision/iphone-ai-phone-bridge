"""Paid synthetic API comparison; never opens a phone or desktop audio device."""
import argparse
import asyncio
from contextlib import suppress
import json
import time
import wave
import aiohttp
import numpy as np
from eleven_store import ElevenStore
from retell_relay import load_proxy
from eleven_dialog import ElevenDialog


async def benchmark(mode, filename):
    with wave.open(filename) as source:
        if source.getframerate()!=16000 or source.getnchannels()!=1:raise ValueError('Use mono PCM 16 kHz WAV')
        audio=np.frombuffer(source.readframes(source.getnframes()),dtype='<i2').astype(np.float32)/32768
    samples=np.repeat(audio,3)
    live=asyncio.Event()
    result={'provider':mode,'syntheticAudioSeconds':round(len(audio)/16000,3),'phoneHardwareUsed':False}
    response_at=None
    class Queue:
        def clear(self):pass
    class Bridge:
        alive=True
        output_queue=Queue()
        def push_audio(self, packet):
            nonlocal response_at
            if response_at is not None and 'afterSpeechToFirstAudioSeconds' not in result:
                if np.max(np.abs(np.frombuffer(packet,dtype='<f4')))>.0001:
                    result['afterSpeechToFirstAudioSeconds']=round(time.perf_counter()-response_at,3)
    async def emit(event):
        if event['type']=='live':live.set()
        if event['type']=='latency':
            result['llmFirstTextSeconds']=round(event['textSeconds'],3) if event['textSeconds'] else None
            result['fromTranscriptToAudioSeconds']=round(event['audioSeconds'],3)
    config=ElevenStore().read()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None,connect=12,sock_read=35)) as http:
        dialog=ElevenDialog(mode,config,Bridge(),http,load_proxy(),emit)
        task=asyncio.create_task(dialog.run())
        start=time.perf_counter()
        async def wait_step():
            if task.done():await task
            await asyncio.sleep(.02)
        try:
            while not live.is_set():
                await wait_step()
                if time.perf_counter()-start>15:raise TimeoutError('Startup')
            result['connectionSeconds']=round(time.perf_counter()-start,3)
            # Let the greeting finish before playing the synthetic caller.
            await asyncio.sleep(1)
            while (dialog.answer and not dialog.answer.done()) or dialog.player.speaking:
                await wait_step()
                if time.perf_counter()-start>20:raise TimeoutError('Greeting')
            pace=time.perf_counter()
            for offset in range(0,len(samples),960):
                await asyncio.sleep(max(0,pace+offset/48000-time.perf_counter()))
                packet=samples[offset:offset+960]
                dialog.feed(np.pad(packet,(0,960-len(packet))).astype('<f4').tobytes())
            response_at=time.perf_counter()
            for index in range(750):
                await asyncio.sleep(max(0,response_at+index*.02-time.perf_counter()))
                dialog.feed(bytes(3840))
                if task.done():await task
                if 'afterSpeechToFirstAudioSeconds' in result:break
            else:raise TimeoutError('Response')
            print(json.dumps(result),flush=True)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):await task


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=('agents','custom'))
    parser.add_argument('audio')
    args=parser.parse_args()
    asyncio.run(benchmark(args.mode,args.audio))
