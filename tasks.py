import os
import argparse
import json
import random
import functools
import itertools
import numpy as np
import torch
import torch.multiprocessing as mp

from tqdm import tqdm
from typing import Iterator, Dict
from torch.utils.data import DataLoader


class IEngine:

    def setup(self, config: dict, device: torch.device, verbose: bool) -> None:
        pass

    def get_dataloader(self, test: bool) -> DataLoader:
        pass

    def load_state(self, path: str) -> None:
        pass

    def save_state(self, path: str) -> None:
        pass

    def train(self, dataloader: DataLoader) -> Iterator[Dict[str, float]]:
        pass

    def eval(self, dataloader: DataLoader) -> Iterator[Dict[str, float]]:
        pass


class Task:

    def __init__(self, config: dict, seed: int, label: str, verbose: bool, engine: IEngine):
        self.config = config
        self.seed = seed
        self.label = label
        self.verbose = verbose
        self.engine = engine

    def __call__(self, device: torch.device):

        self.root = f"outputs/{self.label}"
        self.device = device

        if os.path.exists(f"{self.root}/done"):
            return

        self._setup()

        while self.epoch < self.num_epochs:
            self.epoch += 1

            self._train()
            self._eval()

        self._finish()

    def _setup(self):

        np.random.seed(self.seed)
        torch.manual_seed(self.seed)
        torch.cuda.manual_seed(self.seed)

        self.epoch = 0
        self.num_epochs = self.config["fit"]["epochs"]

        self.engine.setup(self.config, self.device, self.verbose)

        if "source" in self.config["fit"]:
            self.engine.load_state(self.config["fit"]["source"])

    def _finish(self):

        os.makedirs(self.root, exist_ok=True)

        with open(f"{self.root}/config.json", "w")as f:
            json.dump(self.config, f, indent=2)

        self.engine.save_state(f"{self.root}/checkpoint.pt")

        with open(f"{self.root}/done", "w"):
            pass

    def _train(self):

        dataloader = self.engine.get_dataloader(test=False)
        if self.verbose:
            dataloader = tqdm(dataloader, ncols=100, leave=False)

        for metrics in self.engine.train(dataloader):
            if self.verbose:
                dataloader.set_postfix(metrics)

    def _eval(self):

        dataloader = self.engine.get_dataloader(test=True)
        if self.verbose:
            dataloader = tqdm(dataloader, ncols=100, leave=False)

        for metrics in self.engine.eval(dataloader):
            if self.verbose:
                dataloader.set_postfix(metrics)

        status = " ".join([f"{k}={v:.4g}" for k, v in metrics.items()])
        print(f"{self.label}: epoch={self.epoch} {status}")


def run(get_engine):
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", "-p", type=str, default="configs/plan.json")
    parser.add_argument("--groups", "-g", type=str, default="")
    parser.add_argument("--samples", "-s", type=int, default=1)
    parser.add_argument("--threads", "-t", type=int, default=4)
    parser.add_argument("--label", "-l", type=str, default="test")
    parser.add_argument("--device", "-d", type=str, default="cuda")
    parser.add_argument("--verbose", "-v", action="store_true", default=False)
    args = parser.parse_args()

    # load configs
    with open(args.plan) as f:
        plan = json.load(f)
    with open(plan["config"]) as f:
        config = json.load(f)

    # test mode
    if args.groups == "":
        seed = random.getrandbits(31)
        task = Task(config, seed, args.label, args.verbose, get_engine())
        device = torch.device(args.device)
        task(device)
        return

    # build tasks
    tasks = []
    for group in args.groups.split(","):
        dims = build_dims(plan, group, args.samples)
        for comb in itertools.product(*dims):
            values, updates = zip(*comb)
            config_ = functools.reduce(merge, updates, config)
            label = "_".join(values)
            seed = random.getrandbits(31)
            task = Task(config_, seed, label, args.verbose, get_engine())
            tasks.append(task)

    # spawn workers
    mp.set_start_method("spawn")
    queue = mp.Queue()
    workers = []
    devices = [torch.device("cpu")]
    if torch.cuda.is_available():
        num_devices = torch.cuda.device_count()
        devices = [torch.device(i) for i in range(num_devices)]
    for device in devices:
        for _ in range(args.threads):
            worker_args = (queue, device)
            worker = mp.Process(target=worker_fn, args=worker_args)
            workers.append(worker)

    # execute tasks
    for worker in workers:
        worker.start()
    for task in tasks:
        queue.put(task)
    for worker in workers:
        queue.put(None)
    for worker in workers:
        worker.join()


def merge(x, y):
    if not isinstance(x, dict) or not isinstance(y, dict):
        return y
    x = dict(x)
    for k, v in y.items():
        x[k] = merge(x.get(k), v)
    return x


def build_dims(plan, group, num_samples):

    dims = []
    for factor, value in plan["groups"][group].items():
        dim = plan["factors"][factor]
        if isinstance(value, str):
            dims.append([(value, dim[value])])
        elif isinstance(value, list):
            dim = {k: dim[k] for k in value}
            dims.append(dim.items())
        elif value is True:
            dims.append(dim.items())

    samples = {f"{i+1}": {} for i in range(num_samples)}
    dims.append(samples.items())

    return dims


def worker_fn(queue, device):
    while True:
        task = queue.get()
        if task is None:
            break
        try:
            task(device)
        except Exception as e:
            print(e)
