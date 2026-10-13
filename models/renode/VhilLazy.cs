//
// What lazily computed peripherals and the DMA share (#260), compiled by Renode
// at load time before the models that use it (catalog/platforms/
// stm32h733.yaml).
//
// ADC3 (Stm32H7Adc3.cs) can hand its results to the DMA lazily: instead of a
// clock event per result (12.5 kHz on the AMS), it delivers the results due
// by the time anything could see them. Whoever can see them calls CatchUp
// first: the DMA before any of its registers is accessed (Stm32H7Dma.cs), the
// ADC's own registers, the RAM the DMA writes (bus hooks), the DMAMUX. After
// the DMA's configuration changes, Replan lets the ADC decide again how far
// it may run ahead. NoteRequest tells the ADC which stream took its request.
//
using System;
using System.Collections.Generic;
using System.Runtime.CompilerServices;
using Antmicro.Renode.Core;

namespace Antmicro.Renode.Peripherals
{
    // A DMA stream as a lazily delivering peripheral sees it.
    public interface IVhilDmaStream
    {
        // Enabled, peripheral-to-memory, with data items left: each request
        // moves one item now.
        bool TakesRequests { get; }

        // Requests until the next one that sets HTIF or TCIF (at least 1),
        // where the stream's state changes in a way someone may be woken by.
        uint RequestsToNextFlag { get; }

        // Where the next `requests` requests write: [start, start + length).
        bool TryNextWrites(uint requests, out ulong start, out ulong length);

        // Whether the next requests read `source` (a fixed peripheral
        // address: no PINC) `size` bytes at a time, as TakeMany assumes.
        bool ReadsFrom(ulong source, out int size);

        // What `values.Count` requests in a row do, each reading the next
        // value at `source` (ReadsFrom): the same items written, counters,
        // flags and interrupt line, with no flag in between (at most
        // RequestsToNextFlag of them). For a requester that knows the values.
        void TakeMany(IReadOnlyList<uint> values);
    }

    public sealed class VhilLazy
    {
        public static VhilLazy For(IMachine machine)
        {
            lock(perMachine)
            {
                return perMachine.GetValue(machine, _ => new VhilLazy());
            }
        }

        public void Register(Action catchUp, Action replan)
        {
            catchUps.Add(catchUp);
            replans.Add(replan);
        }

        // Before anything observes what a lazy producer may still owe.
        public void CatchUp()
        {
            for(var i = 0; i < catchUps.Count; i++)
            {
                catchUps[i]();
            }
        }

        // After a DMA's configuration changed.
        public void Replan()
        {
            for(var i = 0; i < replans.Count; i++)
            {
                replans[i]();
            }
        }

        // The stream that took the last DMA request (read and cleared by the
        // requester right after it raised one).
        public IVhilDmaStream LastRequest { get; set; }

        private readonly List<Action> catchUps = new List<Action>();
        private readonly List<Action> replans = new List<Action>();

        private static readonly ConditionalWeakTable<IMachine, VhilLazy> perMachine =
            new ConditionalWeakTable<IMachine, VhilLazy>();
    }
}
