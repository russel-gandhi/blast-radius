"""pytest plugin: run process-pool work in-process (with pickling), to save memory."""

def _install_inprocess_pool():
    """Tests hand work to spawned process pools. Run it in-process (same pickling) instead."""
    import concurrent.futures as cf
    import pickle

    def rt(obj):
        return pickle.loads(pickle.dumps(obj))

    def call(fn, args, kwargs):
        return rt(fn(*rt(args), **rt(kwargs)))

    class InProcessPool(cf.ThreadPoolExecutor):
        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
            if initializer is not None:
                init, iargs = initializer, initargs
                initializer, initargs = (lambda: init(*rt(iargs))), ()
            super().__init__(max_workers=1, initializer=initializer, initargs=initargs)

        def submit(self, fn, /, *args, **kwargs):
            return super().submit(call, fn, args, kwargs)

        def map(self, fn, *iterables, timeout=None, chunksize=1, **_):
            return super().map(lambda *a: call(fn, a, {}), *iterables, timeout=timeout)

    cf.ProcessPoolExecutor = InProcessPool



_install_inprocess_pool()
