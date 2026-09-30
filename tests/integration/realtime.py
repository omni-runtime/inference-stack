#!/usr/bin/env python3
"""Run WebSocket transport acceptance independently from HTTP/SSE coverage."""
import argparse
from run import Run
from websocket_cases import realtime_cases

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment',required=True)
    parser.add_argument('--runtime',choices=['kubernetes','docker'],required=True)
    parser.add_argument('--example',required=True)
    parser.add_argument('--catalog',default='tests/overlays/mock/catalog-media.yaml')
    parser.add_argument('--overlay',choices=['mock'],required=True)
    parser.add_argument('--url')
    run=Run(parser.parse_args());run.wait_gateway();realtime_cases(run)
    raise SystemExit(bool(run.collect()))
