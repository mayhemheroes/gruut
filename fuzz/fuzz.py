#!/usr/local/bin/python3
import atheris
import sys

from gruut import sentences


@atheris.instrument_func
def TestOneInput(data):
    fdp = atheris.FuzzedDataProvider(data)
    for sent in sentences(fdp.ConsumeString(len(data)), lang="en-us"):
        for word in sent:
            pass


atheris.instrument_all()
atheris.Setup(sys.argv, TestOneInput)
atheris.Fuzz()