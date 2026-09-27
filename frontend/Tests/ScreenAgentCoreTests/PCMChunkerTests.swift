@preconcurrency import AVFoundation
import Foundation
import Testing

@testable import ScreenAgentCore

@Suite("PCMChunker (MicrophoneCapture's converter)")
struct PCMChunkerTests {
    /// A synthetic mic buffer: a 440 Hz tone in the given format.
    func toneBuffer(sampleRate: Double, channels: AVAudioChannelCount, seconds: Double)
        -> AVAudioPCMBuffer
    {
        let format = AVAudioFormat(
            commonFormat: .pcmFormatFloat32, sampleRate: sampleRate, channels: channels,
            interleaved: false)!
        let frames = AVAudioFrameCount(sampleRate * seconds)
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames)!
        buffer.frameLength = frames
        for channel in 0..<Int(channels) {
            let samples = buffer.floatChannelData![channel]
            for i in 0..<Int(frames) {
                samples[i] = 0.5 * sin(2 * .pi * 440 * Float(i) / Float(sampleRate))
            }
        }
        return buffer
    }

    @Test func produces3200ByteChunksFrom48kHz() throws {
        let input = toneBuffer(sampleRate: 48_000, channels: 1, seconds: 0.1)
        let chunker = try PCMChunker(inputFormat: input.format)
        var chunks: [Data] = []
        for _ in 0..<10 {  // 1 s of audio delivered as ten 100 ms tap callbacks
            chunks += chunker.append(input)
        }
        #expect(!chunks.isEmpty)
        #expect(chunks.allSatisfy { $0.count == PCMChunker.chunkBytes })
        let tail = chunker.flush() ?? Data()
        #expect(tail.count < PCMChunker.chunkBytes && tail.count % 2 == 0)
        // 1 s at 16 kHz mono PCM16 = 32,000 bytes, minus a little resampler latency.
        let total = chunks.count * PCMChunker.chunkBytes + tail.count
        #expect((31_000...32_000).contains(total))
        #expect(chunker.flush() == nil)
    }

    @Test func downmixesStereo44kHz() throws {
        let input = toneBuffer(sampleRate: 44_100, channels: 2, seconds: 0.5)
        let chunker = try PCMChunker(inputFormat: input.format)
        let chunks = chunker.append(input) + chunker.append(input)
        #expect(chunks.count >= 9)
        #expect(chunks.allSatisfy { $0.count == 3_200 })
        // The tone survives conversion (not silence).
        let samples = chunks[2].withUnsafeBytes { Array($0.bindMemory(to: Int16.self)) }
        #expect(samples.map { abs(Int($0)) }.max()! > 10_000)
    }

    @Test func outputIsAlready16kMonoPCM16() {
        #expect(PCMChunker.outputFormat.sampleRate == 16_000)
        #expect(PCMChunker.outputFormat.channelCount == 1)
        #expect(PCMChunker.outputFormat.commonFormat == .pcmFormatInt16)
    }
}
