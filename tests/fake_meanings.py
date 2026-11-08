"""Un finto calcolo dei significati, senza server, per i test di coppermind, Claude e corsie: a ogni
testo un vettore normalizzato che dipende dalle sue parole (testi con parole in comune sono vicini)."""

import hashlib
import re

import numpy


def vector(text):
    out = numpy.zeros(64, dtype=numpy.float32)
    for word in re.findall(r"\w+", text.lower()):
        out[hashlib.md5(word.encode()).digest()[0] % 64] += 1.0
    norm = numpy.linalg.norm(out)
    return out / norm if norm else out


class FakeMeanings:
    name = "finto-modello"

    def __init__(self, fail=None):
        self.fail, self.calls = fail, []

    async def vectors(self, texts, query=False):
        self.calls.append((list(texts), query))
        if self.fail:
            raise self.fail
        return [vector(t) for t in texts]

    async def close(self):
        pass
