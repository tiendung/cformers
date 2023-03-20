# Convert Hugging Face fine-tuned gpt-neox-like models to ggml format
#
# Usage:
#
#   python3 models/convert-h5-to-ggml.py
#
# This script is similar to "convert-pt-to-ggml.py"
#

import io
import os
import sys
import struct
import json
import code
import torch
import numpy as np

from transformers import AutoModelForCausalLM, AutoTokenizer

# ref: https://github.com/openai/gpt-2/blob/master/src/encoder.py
def bytes_to_unicode():
    """
    Returns list of utf-8 byte and a corresponding list of unicode strings.
    The reversible bpe codes work on unicode strings.
    This means you need a large # of unicode characters in your vocab if you want to avoid UNKs.
    When you're at something like a 10B token dataset you end up needing around 5K for decent coverage.
    This is a significant percentage of your normal, say, 32K bpe vocab.
    To avoid that, we want lookup tables between utf-8 bytes and unicode strings.
    And avoids mapping to whitespace/control characters the bpe code barfs on.
    """
    bs = list(range(ord("!"), ord("~")+1))+list(range(ord("¡"), ord("¬")+1))+list(range(ord("®"), ord("ÿ")+1))
    cs = bs[:]
    n = 0
    for b in range(2**8):
        if b not in bs:
            bs.append(b)
            cs.append(2**8+n)
            n += 1
    cs = [chr(n) for n in cs]
    return dict(zip(bs, cs))

if len(sys.argv) < 3:
    print("Usage: python convert-hf-to-ggml.py model_name dir-output [use-f32]")
    print("  model_name: name of the model to convert. Example: 'bigscience/bloomz-560m'")
    print("  dir-output: directory where the output file will be written")
    print("  use-f32:    if present, use float32 instead of float16")
    sys.exit(1)

model_name = sys.argv[1]
dir_out = sys.argv[2]

# make sure the output directory exists
os.makedirs(dir_out, exist_ok=True)

# possible data types
#   ftype == 0 -> float32
#   ftype == 1 -> float16
#
# map from ftype to string
ftype_str = ["f32", "f16"]
ftype = 1
if len(sys.argv) > 3:
    ftype = 0

tokenizer = AutoTokenizer.from_pretrained(model_name)
print("Loading model: ", model_name)
model = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype=torch.float16 if ftype == 1 else torch.float32)
hparams = model.config.to_dict()
print("Model loaded: ", model_name)


fname_out = dir_out + f"/ggml-model-{model_name.split('/')[-1]}-{ftype_str[ftype]}.bin"
fout = open(fname_out, "wb")

hparams["multiple_of"] = 1
fout.write(struct.pack("i", 0x67676d6c)) # magic: ggml in hex
fout.write(struct.pack("i", hparams["vocab_size"]))
# fout.write(struct.pack("i", hparams["seq_length"]))
fout.write(struct.pack("i", hparams["hidden_size"]))
fout.write(struct.pack("i", hparams["num_attention_heads"]))
fout.write(struct.pack("i", hparams["num_hidden_layers"]))
# TODO: Check if this is correct.
fout.write(struct.pack("i", int((hparams["hidden_size"] / hparams["num_attention_heads"]
                             ) * hparams["rotary_pct"]))) # rotary_dim
fout.write(struct.pack("i", int(hparams["use_parallel_residual"])))

fout.write(struct.pack("i", ftype))

# Is this correct??
dot_token = tokenizer.encode(".")[0]
for i in range(hparams["vocab_size"]):
    text = tokenizer.decode([i]).encode('utf-8')
    fout.write(struct.pack("i", len(text)))
    fout.write(text)

list_vars = model.state_dict()

# All the `gpt_neox.layers.<LAYER_ID>.attention.query_key_value.weight` layers
# should be split into 3 layers:
#  gpt_neox.layers.<LAYER_ID>.attention.query.weight
#  gpt_neox.layers.<LAYER_ID>.attention.key.weight
#  gpt_neox.layers.<LAYER_ID>.attention.value.weight
# Similarly split `gpt_neox.layers.<LAYER_ID>.attention.query_key_value.bias`.
new_list_vars = {}
for name in list_vars.keys():
    if name.startswith('gpt_neox.layers.') and \
            name.endswith('attention.query_key_value.weight'):
        new_list_vars[name.replace('query_key_value.weight', 'query.weight')] = \
                list_vars[name][:hparams['hidden_size'], :]
        new_list_vars[name.replace('query_key_value.weight', 'key.weight')] = \
                list_vars[name][hparams['hidden_size']:2*hparams['hidden_size'], :]
        new_list_vars[name.replace('query_key_value.weight', 'value.weight')] = \
                list_vars[name][2*hparams['hidden_size']:, :]
    elif name.startswith('gpt_neox.layers.') and \
            name.endswith('attention.query_key_value.bias'):
        new_list_vars[name.replace('query_key_value.bias', 'query.bias')] = \
                list_vars[name][:hparams['hidden_size']]
        new_list_vars[name.replace('query_key_value.bias', 'key.bias')] = \
                list_vars[name][hparams['hidden_size']:2*hparams['hidden_size']]
        new_list_vars[name.replace('query_key_value.bias', 'value.bias')] = \
                list_vars[name][2*hparams['hidden_size']:]
    else:
        new_list_vars[name] = list_vars[name]
list_vars = new_list_vars

for name in list_vars.keys():
    if name.startswith('gpt_neox.layers.'):
        if 'attention.masked_bias' in name or \
            'attention.rotary_emb.inv_freq' in name or \
            'attention.bias' in name:
            continue
    src = name
    nn = name

    print(src, ' -> ', name)
    data = list_vars[src].squeeze().numpy()
    data = data.astype(np.float32)

    n_dims = len(data.shape)
    print(name, n_dims, data.shape)

    # default type is fp32
    ftype_cur = 0
    if ftype == 1 and n_dims > 1:
        print("  Converting to float16", data.shape, data[:3, :3].tolist())
        data = data.astype(np.float16)
        ftype_cur = 1

    # header
    str = name.encode('utf-8')
    fout.write(struct.pack("iii", n_dims, len(str), ftype_cur))
    for i in range(n_dims):
        fout.write(struct.pack("i", data.shape[n_dims - 1 - i]))
    print(str)
    fout.write(str)

    # data
    data.tofile(fout)

fout.close()

print("Done. Output file: " + fname_out)
print("")
