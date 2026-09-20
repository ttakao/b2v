"""Validate PCM WAV responses before storing or joining them."""
import io
import wave


def inspect_wav(data):
    try:
        with wave.open(io.BytesIO(data), 'rb') as wav:
            channels, width, rate, frames = wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()
            if channels < 1 or width not in (1,2,3,4) or rate <= 0 or frames <= 0 or wav.getcomptype()!='NONE':
                raise ValueError('WAV形式が不正です。')
            if len(wav.readframes(frames)) != frames*channels*width:
                raise ValueError('WAVデータが途中で切れています。')
            return {'sample_rate':rate, 'channels':channels, 'sample_width':width, 'duration':frames/rate,
                    'dtype':f'PCM{width*8}', 'size_bytes':len(data)}
    except (wave.Error, EOFError) as exc:
        raise ValueError('有効なWAV音声を取得できませんでした。') from exc
