#coding: utf-8

import os
import os.path as osp
import time
import random
import numpy as np
import random
import soundfile as sf

import torch
from torch import nn
import torch.nn.functional as F
import torchaudio
from torch.utils.data import DataLoader
import librosa
from nltk.tokenize import word_tokenize
import phonetisaurus

import re
import logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
from text_utils import TextCleaner
np.random.seed(1)
random.seed(1)
DEFAULT_DICT_PATH = osp.join(osp.dirname(__file__), 'word_index_dict.txt')
SPECT_PARAMS = {
    "n_fft": 2048,
    "win_length": 1200,
    "hop_length": 300
}
MEL_PARAMS = {
    "n_mels": 80,
    "n_fft": 2048,
    "win_length": 1200,
    "hop_length": 300
}

import os

os.environ['PHONEMIZER_ESPEAK_LIBRARY'] = '/home/lemoi18/StyleTTS2/Modules/espeak-ng/build/src/libespeak-ng/libespeak-ng.so.1.52.0.1'
import phonemizer
global_phonemizer = phonemizer.backend.EspeakBackend(language='nb', preserve_punctuation=True,  with_stress=True)
class MelDataset(torch.utils.data.Dataset):
    def __init__(self,
                 data_list,
                 dict_path=DEFAULT_DICT_PATH,
                 sr=24000
                ):

        spect_params = SPECT_PARAMS
        mel_params = MEL_PARAMS

        _data_list = [l.split('|') for l in data_list]
        self.data_list = [data if len(data) == 3 else (data[0], data[1], '0') for data in _data_list]
        self.text_cleaner = TextCleaner(dict_path)
        self.sr = sr

        self.to_melspec = torchaudio.transforms.MelSpectrogram(**MEL_PARAMS)
        self.mean, self.std = -4, 4
        

    def __len__(self):
        return len(self.data_list)

    def __getitem__(self, idx):
        data = self.data_list[idx]
        wave, text_tensor, speaker_id = self._load_tensor(data)
        wave_tensor = torch.from_numpy(wave).float()
        mel_tensor = self.to_melspec(wave_tensor)

        if (text_tensor.size(0)+1) >= (mel_tensor.size(1) // 3):
            mel_tensor = F.interpolate(
                mel_tensor.unsqueeze(0), size=(text_tensor.size(0)+1)*3, align_corners=False,
                mode='linear').squeeze(0)

        acoustic_feature = (torch.log(1e-5 + mel_tensor) - self.mean)/self.std

        length_feature = acoustic_feature.size(1)
        acoustic_feature = acoustic_feature[:, :(length_feature - length_feature % 2)]

        return wave_tensor, acoustic_feature, text_tensor, data[0]

    def _load_tensor(self, data):
        wave_path, text, speaker_id = data
    
        try:
            # Convert speaker_id to integer
            speaker_id = int(speaker_id)
        except ValueError as e:
            raise ValueError(f"Invalid speaker_id: {speaker_id}. Error: {e}")
    
        # Load audio file
        wave, sr = sf.read(wave_path)
    
        # Convert to mono if stereo
        if wave.ndim > 1:
            wave = np.mean(wave, axis=1)
    
        # Resample to 24kHz if necessary
        if sr != 24000:
            wave = librosa.resample(wave, orig_sr=sr, target_sr=24000)
    
        # Tokenize text while preserving punctuation
        tokens = re.findall(r"[\w']+|[.,!?;:]", text)
    
        # Transcribe the entire sentence at once using the G2P model
        result = list(transcribe(' '.join([t for t in tokens if re.match(r"[\w']+", t)])))  # Pass only words to `transcribe`
        
        # Prepare to merge transcriptions and punctuation
        transcription = []
        result_index = 0
    
        for token in tokens:
            if re.match(r"[.,!?;:]", token):  # If token is punctuation
                # Append punctuation to the last phonetic transcription in the list
                if transcription:
                    transcription[-1] += token
            else:  # Otherwise, it's a word
                t, phonetic = result[result_index]
                #print(t,phonetic)
                transcription.extend(phonetic.split())  # Split phonetic into individual elements and add to transcription
                result_index += 1
    
        # Join transcription as a single string for further processing
        ps = ' '.join(transcription)
        #print(transcription)
        # Clean text and convert to indices
        text_indices = self.text_cleaner(ps)

        #print(text_indices)
        blank_index = self.text_cleaner.word_index_dictionary[" "]
        text_indices.insert(0, blank_index)  # Add silence at the beginning
        text_indices.append(blank_index)     # Add silence at the end
    
        # Convert text to tensor
        text_tensor = torch.LongTensor(text_indices)
    
        return wave, text_tensor, speaker_id
        

def transcribe_words(words, dialect='e', style="written"):
    transcriptions = phonetisaurus.predict(words, model_path="/home/lemoi18/G2P-no/models/nb_e_written.fst")
    return transcriptions

def format_transcription(pronunciation):
    return " ".join(pronunciation)

def transcribe(text, dialect='e', style="written"):
    words = text.split()
    transcriptions = transcribe_words(words, dialect=dialect, style=style)
    return [(word, format_transcription(pron)) for word, pron in transcriptions]

class Collater(object):
    """
    Args:
      return_wave (bool): if true, will return the wave data along with spectrogram. 
    """

    def __init__(self, return_wave=False):
        self.text_pad_index = 0
        self.return_wave = return_wave

    def __call__(self, batch):
        batch_size = len(batch)

        # sort by mel length
        lengths = [b[1].shape[1] for b in batch]
        batch_indexes = np.argsort(lengths)[::-1]
        batch = [batch[bid] for bid in batch_indexes]

        nmels = batch[0][1].size(0)
        max_mel_length = max([b[1].shape[1] for b in batch])
        max_text_length = max([b[2].shape[0] for b in batch])

        mels = torch.zeros((batch_size, nmels, max_mel_length)).float()
        texts = torch.zeros((batch_size, max_text_length)).long()
        input_lengths = torch.zeros(batch_size).long()
        output_lengths = torch.zeros(batch_size).long()
        paths = ['' for _ in range(batch_size)]
        for bid, (_, mel, text, path) in enumerate(batch):
            mel_size = mel.size(1)
            text_size = text.size(0)
            mels[bid, :, :mel_size] = mel
            texts[bid, :text_size] = text
            input_lengths[bid] = text_size
            output_lengths[bid] = mel_size
            paths[bid] = path
            assert(text_size < (mel_size//2))

        if self.return_wave:
            waves = [b[0] for b in batch]
            return texts, input_lengths, mels, output_lengths, paths, waves

        return texts, input_lengths, mels, output_lengths



def build_dataloader(path_list,
                     validation=False,
                     batch_size=4,
                     num_workers=1,
                     device='cuda',
                     collate_config={},
                     dataset_config={}):

    dataset = MelDataset(path_list, **dataset_config)
    collate_fn = Collater(**collate_config)
    data_loader = DataLoader(dataset,
                             batch_size=batch_size,
                             shuffle=(not validation),
                             num_workers=num_workers,
                             drop_last=(not validation),
                             collate_fn=collate_fn,
                             pin_memory=(device != 'cpu'))

    return data_loader