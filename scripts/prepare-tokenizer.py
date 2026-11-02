"""Install the official tokenizer locally; it is not included in this release."""
import argparse
import io
from pathlib import Path
import urllib.request
import zipfile

URL = 'https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip'


def install(archive, destination):
    from tokenizers import Tokenizer
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        names = [n for n in source.namelist() if Path(n).name == 'tokenizer.json']
        if len(names) != 1:
            raise ValueError('Expected exactly one tokenizer.json in the official archive')
        raw = source.read(names[0])
    Tokenizer.from_str(raw.decode('utf-8'))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)
    print('Tokenizer installed:', destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, help='Previously downloaded official ZIP')
    args = parser.parse_args()
    destination = Path(__file__).resolve().parents[1] / 'src/dfx_agent/data/deepseek-v4/tokenizer.json'
    if destination.exists():
        from tokenizers import Tokenizer
        Tokenizer.from_file(str(destination))
        print('Existing tokenizer is readable:', destination)
        return
    if args.archive:
        raw = args.archive.read_bytes()
    else:
        print('Downloading official tokenizer:', URL, flush=True)
        with urllib.request.urlopen(URL, timeout=120) as response:
            raw = response.read()
    install(raw, destination)


if __name__ == '__main__':
    main()
