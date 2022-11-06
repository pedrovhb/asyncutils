from __future__ import annotations

import asyncio
import inspect
from typing import (
    Any,
    AsyncIterable,
    AsyncIterator,
    Callable,
    cast,
    Coroutine,
    Iterable,
    Iterator,
    overload,
    Protocol,
    Sequence,
    TypeAlias,
    TypeVar,
)

from astream.closeable_queue import CloseableQueue, QueueExhausted
from astream.utils import ensure_async_iterator, ensure_coro_fn

_NextT = TypeVar("_NextT")
_T = TypeVar("_T")

_T_co = TypeVar("_T_co", covariant=True)

Coro: TypeAlias = Coroutine[Any, Any, _T]


class IsIterable(Protocol[_T_co]):
    """A protocol for objects that can be converted to an async iterable."""

    def __iter__(self) -> Iterator[_T_co]:
        ...


async def _identity(x: _T) -> _T:
    return x


async def _flatten(iterable: AsyncIterable[Iterable[_T]]) -> AsyncIterator[_T]:
    """Flatten an async iterable of async iterables."""
    async for it in iterable:
        for item in it:
            yield item


async def _amap(
    fn: Callable[[_T], Coro[_NextT]] | Callable[[_T], _NextT],
    iterable: AsyncIterable[_T],
) -> AsyncIterator[_NextT]:
    """An asynchronous version of `map`."""
    _async_fn = ensure_coro_fn(fn)
    async for item in iterable:
        yield await _async_fn(item)


async def _afilter(
    fn: Callable[[_T], Coro[bool]] | Callable[[_T], bool],
    iterable: AsyncIterable[_T],
) -> AsyncIterator[_T]:
    """An asynchronous version of `filter`."""
    _async_fn = ensure_coro_fn(fn)
    async for item in iterable:
        if await _async_fn(item):
            yield item


async def _aflatmap(
    fn: Callable[[_T], Iterable[_NextT]]
    | Callable[[_T], Coro[Iterable[_NextT]]]
    | Callable[[_T], AsyncIterable[_NextT]],
    iterable: AsyncIterable[_T],
) -> AsyncIterator[_NextT]:
    """An asynchronous version of `flatmap`."""

    # If the function is an async generator, yield from it.
    if inspect.isasyncgenfunction(fn):
        _async_gen = cast(Callable[[_T], AsyncIterable[_NextT]], fn)
        async for item in iterable:
            async for subitem in _async_gen(item):
                yield subitem

    # If it returns an iterable, we flatten it
    else:
        _coro = ensure_coro_fn(fn)
        _async_fn = cast(Callable[[_T], Coro[Iterable[_NextT]]], _coro)
        async for item in iterable:
            for subitem in await _async_fn(item):
                yield subitem


class Stream(AsyncIterator[_T]):
    def __init__(  # noqa
        self,
        iterable: AsyncIterable[_T] | Iterable[_T],
        max_out_q_size: int = 0,
        start: bool = True,
    ) -> None:
        self._out_q: CloseableQueue[_T] = CloseableQueue(maxsize=max_out_q_size)

        self._started = asyncio.Event()
        self._closed = asyncio.Event()

        self._async_iterator = ensure_async_iterator(iterable, to_thread=True)

        if start:
            self.start()

    def start(self) -> None:
        if self._started.is_set():
            raise RuntimeError("Already started.")
        self._started.set()
        asyncio.create_task(self._feed(self._async_iterator))

    async def _feed(self, async_iterator: AsyncIterator[_T]) -> None:
        async for item in async_iterator:
            await self._out_q.put(item)
        self._out_q.close()

    async def wait_started(self) -> None:
        await self._started.wait()

    async def wait_exhausted(self) -> None:
        await self._out_q.wait_exhausted()

    async def wait_closed(self) -> None:
        await self._out_q.wait_closed()

    async def gather(self) -> list[_T]:
        return [it async for it in self]

    async def acollect(self, fn: Callable[[Sequence[_T]], Coro[_NextT]]) -> _NextT:
        # todo - take max_out_q_size into account, accept sync fn
        return await fn([it async for it in self])

    @overload
    def amap(self, fn: Callable[[_T], Coro[_NextT]]) -> Stream[_NextT]:
        ...

    @overload
    def amap(self, fn: Callable[[_T], _NextT]) -> Stream[_NextT]:
        ...

    def amap(self, fn: Any) -> Stream[_NextT]:
        return Stream(
            _amap(fn, self),
            max_out_q_size=self._out_q.maxsize,
            start=self._started.is_set(),
        )

    @overload
    def afilter(self, fn: Callable[[_T], Coro[bool]]) -> Stream[_T]:
        ...

    @overload
    def afilter(self, fn: Callable[[_T], bool]) -> Stream[_T]:
        ...

    def afilter(self, fn: Any) -> Stream[_T]:
        return Stream(
            _afilter(fn, self),
            max_out_q_size=self._out_q.maxsize,
            start=self._started.is_set(),
        )

    @overload
    def aflatmap(self, fn: Callable[[_T], Coro[Iterable[_NextT]]]) -> Stream[_NextT]:
        ...

    @overload
    def aflatmap(self, fn: Callable[[_T], AsyncIterable[_NextT]]) -> Stream[_NextT]:
        ...

    @overload
    def aflatmap(self, fn: Callable[[_T], Iterable[_NextT]]) -> Stream[_NextT]:
        ...

    def aflatmap(self, fn: Any) -> Stream[_NextT]:
        return Stream(
            _aflatmap(fn, self),
            max_out_q_size=self._out_q.maxsize,
            start=self._started.is_set(),
        )

    def flatten(self: Stream[Iterable[_NextT]]) -> Stream[_NextT]:
        return Stream(
            _aflatmap(lambda x: x, self),
            max_out_q_size=self._out_q.maxsize,
            start=self._started.is_set(),
        )

    async def __anext__(self) -> _T:
        try:
            item = await self._out_q.get()
            self._out_q.task_done()
            return item
        except QueueExhausted:
            raise StopAsyncIteration

    __truediv__ = amap  # Stream(range(10)) / lambda x: x * 2  --> 0, 2, 4, 6, 8, ...
    __floordiv__ = aflatmap  # Stream(range(10)) // lambda x: [x, x * 2]  --> 0, 0, 1, 2, 2, 4, ...
    __mod__ = afilter  # Stream(range(10)) % lambda x: x % 2 == 0  --> 0, 2, 4, 6, 8, ...
    __pos__ = flatten  # +Stream([[0, 1], [2, 3]]) --> 0, 1, 2, 3
    __matmul__ = acollect  # Stream(range(10)) @ sum --> 45

    # todo - agather with @


__all__ = ("Stream",)
