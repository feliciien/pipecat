import asyncio
import aiohttp

from pipecat.frames.frames import EndFrame, TextFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask
from pipecat.pipeline.runner import PipelineRunner
from pipecat.services.elevenlabs import ElevenLabsTTSService
from pipecat.transports.services.daily import DailyParams, DailyTransport

async def main():
    async with aiohttp.ClientSession() as session:
        # Use Daily as a real-time media transport (WebRTC)
        transport = DailyTransport(
            room_url="https://iitianfreelancer.daily.co/nukYfIESlH3etIODOGcW",
            token= "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyIjoibnVrWWZJRVNsSDNldElPRE9HY1ciLCJkIjoiNWViYTdhMTItNjVmNy00YTA3LWJlNzgtYjg4OTFiMzdiOTNhIiwiaWF0IjoxNzE4NTQ3MzE1fQ.5rNSJDYwvQsFw-9lypgPXqBdwUrXMqwzbBNoVHKkXqg",
            bot_name="bot name",
            params=DailyParams(audio_out_enabled=True)
        )

        # Use Eleven Labs for Text-to-Speech
        tts = ElevenLabsTTSService(
            aiohttp_session=session,
            api_key="sk_359dbe09f0e6ed908d4a910fe95ca2d17a4ee0aee8f07456",
            voice_id="7p1Ofvcwsv7UBPoFNcpI"
        )

        # Simple pipeline that will process text to speech and output the result
        pipeline = Pipeline([tts, transport.output()])

        # Create Pipecat processor that can run one or more pipelines tasks
        runner = PipelineRunner()

        # Assign the task callable to run the pipeline
        task = PipelineTask(pipeline)

        # Register an event handler to play audio when a
        # participant joins the transport WebRTC session
        @transport.event_handler("on_participant_joined")
        async def on_new_participant_joined(transport, participant):
            participant_name = participant["info"]["userName"] or ''
            # Queue a TextFrame that will get spoken by the TTS service (Eleven Labs)
            await task.queue_frames([TextFrame(f"Hello there, {participant_name}!"), EndFrame()])

        # Run the pipeline task
        await runner.run(task)

if _name_ == "_main_":
    asyncio.run(main())