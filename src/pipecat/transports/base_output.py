#
# Copyright (c) 2024, Daily

#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import itertools
import queue
import time
import threading

from concurrent.futures import ThreadPoolExecutor

from PIL import Image
from typing import List

from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    MetricsFrame,
    SpriteFrame,
    StartFrame,
    EndFrame,
    Frame,
    ImageRawFrame,
    StartInterruptionFrame,
    StopInterruptionFrame,
    SystemFrame,
    TransportMessageFrame)
from pipecat.transports.base_transport import TransportParams

from loguru import logger


class BaseOutputTransport(FrameProcessor):

    def __init__(self, params: TransportParams):
        super().__init__()

        self._params = params

        self._running = False

        self._executor = ThreadPoolExecutor(max_workers=5)

        # These are the images that we should send to the camera at our desired
        # framerate.
        self._camera_images = None

        # Create media threads queues.
        if self._params.camera_out_enabled:
            self._camera_out_queue = queue.Queue()
        self._sink_queue = queue.Queue()
        self._sink_thread = None

        self._stopped_event = asyncio.Event()
        self._is_interrupted = threading.Event()

        # We will send 10ms audio which is the smallest possible amount of audio
        # we can send. If we receive long audio frames we will chunk them into
        # 10ms. This will help with interruption handling.
        self._audio_chunk_size = int(self._params.audio_out_sample_rate / 100) * \
            self._params.audio_out_channels * 2

        # Create push frame task. This is the task that will push frames in
        # order. We also guarantee that all frames are pushed in the same task.
        self._create_push_task()

    async def start(self, frame: StartFrame):
        if self._running:
            return

        self._running = True

        loop = self.get_event_loop()

        # Create queues and threads.
        if self._params.camera_out_enabled:
            self._camera_out_thread = loop.run_in_executor(
                self._executor, self._camera_out_thread_handler)

        self._sink_thread = loop.run_in_executor(self._executor, self._sink_thread_handler)

    async def stop(self):
        if not self._running:
            return

        # This will exit all threads.
        self._running = False

        self._stopped_event.set()

    def send_message(self, frame: TransportMessageFrame):
        pass

    def send_metrics(self, frame: MetricsFrame):
        pass

    def write_frame_to_camera(self, frame: ImageRawFrame):
        pass

    def write_raw_audio_frames(self, frames: bytes):
        pass

    #
    # Frame processor
    #

    async def cleanup(self):
        # Wait on the threads to finish.
        if self._params.camera_out_enabled:
            await self._camera_out_thread

        if self._sink_thread:
            await self._sink_thread

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        #
        # Out-of-band frames like (CancelFrame or StartInterruptionFrame) are
        # pushed immediately. Other frames require order so they are put in the
        # sink queue.
        #
        if isinstance(frame, StartFrame):
            await self.start(frame)
            await self.push_frame(frame, direction)
        # EndFrame is managed in the queue handler.
        elif isinstance(frame, CancelFrame):
            await self.stop()
            await self.push_frame(frame, direction)
        elif isinstance(frame, StartInterruptionFrame) or isinstance(frame, StopInterruptionFrame):
            await self._handle_interruptions(frame)
            await self.push_frame(frame, direction)
        elif isinstance(frame, SystemFrame):
            await self.push_frame(frame, direction)
        elif isinstance(frame, AudioRawFrame):
            await self._handle_audio(frame)
        else:
            self._sink_queue.put_nowait(frame)

        # If we are finishing, wait here until we have stopped, otherwise we might
        # close things too early upstream. We need this event because we don't
        # know when the internal threads will finish.
        if isinstance(frame, CancelFrame) or isinstance(frame, EndFrame):
            await self._stopped_event.wait()

    async def _handle_interruptions(self, frame: Frame):
        if not self.interruptions_allowed:
            return

        if isinstance(frame, StartInterruptionFrame):
            self._is_interrupted.set()
            self._push_frame_task.cancel()
            self._create_push_task()
        elif isinstance(frame, StopInterruptionFrame):
            self._is_interrupted.clear()

    async def _handle_audio(self, frame: AudioRawFrame):
        audio = frame.audio
        for i in range(0, len(audio), self._audio_chunk_size):
            chunk = AudioRawFrame(audio[i: i + self._audio_chunk_size],
                                  sample_rate=frame.sample_rate, num_channels=frame.num_channels)
            self._sink_queue.put_nowait(chunk)

    def _sink_thread_handler(self):
        # Audio accumlation buffer
        buffer = bytearray()
        while self._running:
            try:
                frame = self._sink_queue.get(timeout=1)
                if not self._is_interrupted.is_set():
                    if isinstance(frame, AudioRawFrame) and self._params.audio_out_enabled:
                        buffer.extend(frame.audio)
                        buffer = self._maybe_send_audio(buffer)
                    elif isinstance(frame, ImageRawFrame) and self._params.camera_out_enabled:
                        self._set_camera_image(frame)
                    elif isinstance(frame, SpriteFrame) and self._params.camera_out_enabled:
                        self._set_camera_images(frame.images)
                    elif isinstance(frame, TransportMessageFrame):
                        self.send_message(frame)
                    elif isinstance(frame, MetricsFrame):
                        self.send_metrics(frame)
                    else:
                        future = asyncio.run_coroutine_threadsafe(
                            self._internal_push_frame(frame), self.get_event_loop())
                        future.result()
                else:
                    # If we get interrupted just clear the output buffer.
                    buffer = bytearray()

                if isinstance(frame, EndFrame):
                    future = asyncio.run_coroutine_threadsafe(self.stop(), self.get_event_loop())
                    future.result()

                self._sink_queue.task_done()
            except queue.Empty:
                pass
            except BaseException as e:
                logger.error(f"Error processing sink queue: {e}")

    #
    # Push frames task
    #

    def _create_push_task(self):
        loop = self.get_event_loop()
        self._push_frame_task = loop.create_task(self._push_frame_task_handler())
        self._push_queue = asyncio.Queue()

    async def _internal_push_frame(
            self,
            frame: Frame | None,
            direction: FrameDirection | None = FrameDirection.DOWNSTREAM):
        await self._push_queue.put((frame, direction))

    async def _push_frame_task_handler(self):
        while True:
            try:
                (frame, direction) = await self._push_queue.get()
                await self.push_frame(frame, direction)
            except asyncio.CancelledError:
                break

    #
    # Camera out
    #

    async def send_image(self, frame: ImageRawFrame | SpriteFrame):
        await self.process_frame(frame, FrameDirection.DOWNSTREAM)

    def _draw_image(self, frame: ImageRawFrame):
        desired_size = (self._params.camera_out_width, self._params.camera_out_height)

        if frame.size != desired_size:
            image = Image.frombytes(frame.format, frame.size, frame.image)
            resized_image = image.resize(desired_size)
            logger.warning(
                f"{frame} does not have the expected size {desired_size}, resizing")
            frame = ImageRawFrame(resized_image.tobytes(), resized_image.size, resized_image.format)

        self.write_frame_to_camera(frame)

    def _set_camera_image(self, image: ImageRawFrame):
        if self._params.camera_out_is_live:
            self._camera_out_queue.put_nowait(image)
        else:
            self._camera_images = itertools.cycle([image])

    def _set_camera_images(self, images: List[ImageRawFrame]):
        self._camera_images = itertools.cycle(images)

    def _camera_out_thread_handler(self):
        while self._running:
            try:
                if self._params.camera_out_is_live:
                    image = self._camera_out_queue.get(timeout=1)
                    self._draw_image(image)
                    self._camera_out_queue.task_done()
                elif self._camera_images:
                    image = next(self._camera_images)
                    self._draw_image(image)
                    time.sleep(1.0 / self._params.camera_out_framerate)
                else:
                    time.sleep(1.0 / self._params.camera_out_framerate)
            except queue.Empty:
                pass
            except Exception as e:
                logger.error(f"Error writing to camera: {e}")

    #
    # Audio out
    #

    async def send_audio(self, frame: AudioRawFrame):
        await self.process_frame(frame, FrameDirection.DOWNSTREAM)

    def _maybe_send_audio(self, buffer: bytearray) -> bytearray:
        try:
            if len(buffer) >= self._audio_chunk_size:
                self.write_raw_audio_frames(bytes(buffer[:self._audio_chunk_size]))
                buffer = buffer[self._audio_chunk_size:]
            return buffer
        except BaseException as e:
            logger.error(f"Error writing audio frames: {e}")
            return buffer
