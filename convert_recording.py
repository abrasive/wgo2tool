from dataclasses import dataclass
import struct
import wave
import tempfile
import datetime
from pathlib import Path
import os.path
import subprocess

@dataclass
class Page:
    stream_serial: int
    segments: list[bytearray]

    continued_packet: bool
    begin_of_stream: bool
    end_of_stream: bool


def page_reader(stream):
    buffer = bytearray()

    while len(new := stream.read(8192)) or (len(buffer) and b'OggS' in buffer):
        buffer += new

        sync_pos = buffer.find(b'OggS')
        if sync_pos < 0:
            continue

        buffer = buffer[sync_pos:]
        if len(buffer) < 27:
            continue

        if buffer[4] != 0:
            raise ValueError(f'Unknown stream structure revision 0x{buffer[4]:x}')

        stream_serial, = struct.unpack('<L', buffer[14:18])
        flags = buffer[5]

        num_segments = buffer[26]
        if len(buffer) < 27 + num_segments:
            continue

        raw_segment_lengths = buffer[27:27+num_segments]
        segment_lengths = []
        cur_length = 0
        for rsl in raw_segment_lengths:
            if rsl == 0xff:
                cur_length += rsl
            else:
                segment_lengths.append(rsl + cur_length)
                cur_length = 0

        payload_length = sum(segment_lengths)
        if len(buffer) < 27 + num_segments + payload_length:
            continue

        buffer = buffer[27 + num_segments:]

        segments = []
        for segment_length in segment_lengths:
            segments.append(buffer[:segment_length])
            buffer = buffer[segment_length:]

        yield Page(
                stream_serial = stream_serial,
                segments = segments,
                continued_packet = bool(flags & 1),
                begin_of_stream = bool(flags & 2),
                end_of_stream = bool(flags & 4),
                )

class CodecHandler(object):
    @classmethod
    def for_codec(cls, codec_id: bytes, output: dict):
        if codec_id == b'PCM     ':
            return OggPCMHandler(output)
        elif codec_id == b'RODEWgo2':
            return W2GoHandler(output)
        else:
            raise ValueError(f"Unknown codec {codec_id}")

    def handle_packet(self, _):
        pass

class OggPCMHandler(CodecHandler):
    def __init__(self, output):
        self.output = output
        self.num_extra_header_packets = 0
        self.done_packets = 0

    def handle_packet(self, packet):
        if self.done_packets == 0:
            self.handle_header(packet)

        elif self.done_packets <= 1 + self.num_extra_header_packets:
            pass

        else:
            self.wav.writeframes(packet)

        self.done_packets += 1

    def handle_header(self, packet):
        major, minor, pcm_format, sampling_rate, num_bits, num_channels, max_frames_per_packet, num_extra_header_packets = struct.unpack('<HHLLBBHL', packet[8:])
        assert major == 0
        assert minor == 0
        assert pcm_format == 4  # 24-bit

        wavefile = tempfile.NamedTemporaryFile(suffix='.wav')
        self.wav = wave.open(wavefile, 'w')


        self.num_extra_header_packets = num_extra_header_packets

        self.wav.setnchannels(num_channels)
        self.wav.setsampwidth(3)
        self.wav.setframerate(sampling_rate)

        self.output['wave'] = wavefile


class W2GoHandler(CodecHandler):
    def __init__(self, output):
        self.output = output
        self.done_packets = 0
        self.buffer = bytearray()
        self.seconds = 0
        self.markers = []

    def handle_packet(self, packet):
        if self.done_packets == 0:
            self.handle_header(packet)
        elif self.type == 'status':
            self.handle_status(packet)

        self.done_packets += 1

    def handle_header(self, packet):
        if packet[8:9] == b'P':
            self.type = 'peak'
        elif packet[8:9] == b'S':
            self.type = 'status'
            self.output['markers'] = self.markers
        else:
            raise ValueError(f'Unknown Wgo2 packet type {packet}')

    def handle_status(self, packet):
        self.buffer.extend(packet)

        while len(self.buffer) > 16:
            status, self.buffer = self.buffer[:16], self.buffer[16:]

            if status[4] != 0xff and status[4] & 4:
                self.markers.append(self.seconds)

            self.seconds += 1

def decode(fp):
    handlers = {}
    output = {}

    reader = page_reader(fp)
    for page in reader:
        if page.continued_packet:
            raise ValueError("I don't know how to reassemble split packets")

        if page.begin_of_stream:
            codec = bytes(page.segments[0][:8])
            handlers[page.stream_serial] = CodecHandler.for_codec(codec, output)

        handler = handlers[page.stream_serial]

        for segment in page.segments:
            handler.handle_packet(segment)

        if page.end_of_stream:
            handlers.pop(page.stream_serial)

    return output

def make_cuesheet(markers):
    fp = tempfile.NamedTemporaryFile(suffix='.cue', mode='w')

    fp.write('FILE w2go.flac WAVE\n')

    track_start_sec = [0] + markers
    for track, start in enumerate(track_start_sec, 1):
        minutes = start // 60
        seconds = start % 60

        fp.write(f'  TRACK {track:02d} AUDIO\n')
        fp.write(f'    INDEX 01 {minutes:02d}:{seconds:02d}:00\n')

    fp.flush()
    return fp

def convert_ugg(ugg_filename):
    ugg_filename = Path(ugg_filename)
    egg_name = 'PEA' + ugg_filename.name.removeprefix('REC').removesuffix('UGG') + 'EGG'
    egg_filename = ugg_filename.parent / egg_name

    with open(ugg_filename, 'rb') as fp:
        out = decode(fp)
        wavefile = out['wave']

    with open(egg_filename, 'rb') as fp:
        out = decode(fp)
        markers = out['markers']

    if len(markers):
        cuesheet = make_cuesheet(markers)
    else:
        cuesheet = None

    recording_timestamp = os.path.getctime(ugg_filename)
    local_timezone = datetime.datetime.now(datetime.timezone.utc).astimezone().tzinfo
    recording_datetime = datetime.datetime.fromtimestamp(recording_timestamp, tz=local_timezone)

    # flac copies the times, so we can set them here
    os.utime(wavefile.name, (recording_timestamp, recording_timestamp))

    cmd = [
        'flac',
        wavefile.name,
        '-o', 'test.flac',
        '-T', 'DATE=' + recording_datetime.isoformat(),
        '--silent',
        ]

    if cuesheet:
        cmd.extend(['--cuesheet', cuesheet.name])

    subprocess.check_call(cmd)

if __name__ == "__main__":
    convert_ugg('example/REC00005.UGG')
