@preconcurrency import AVFoundation

/// Converts microphone buffers (any rate, channel count, sample format) into
/// 16 kHz mono PCM16 little-endian chunks of exactly `chunkBytes` (100 ms).
final class PCMChunker: @unchecked Sendable {
    static let sampleRate: Double = 16_000
    static let chunkBytes = 3_200  // 100 ms * 16 kHz * 2 bytes
    static let outputFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16, sampleRate: sampleRate, channels: 1, interleaved: true)!

    private let converter: AVAudioConverter
    private let lock = NSLock()
    private var pending = Data()  // guarded by lock

    init(inputFormat: AVAudioFormat) throws {
        guard let converter = AVAudioConverter(from: inputFormat, to: Self.outputFormat) else {
            throw AudioCaptureError.unsupportedFormat(inputFormat.description)
        }
        // Mix all input channels into the mono output instead of keeping only channel 0.
        converter.downmix = true
        self.converter = converter
    }

    /// Converts `buffer` and returns every complete chunk now available.
    func append(_ buffer: AVAudioPCMBuffer) -> [Data] {
        lock.withLock {
            pending.append(convert(buffer))
            var chunks: [Data] = []
            while pending.count >= Self.chunkBytes {
                chunks.append(Data(pending.prefix(Self.chunkBytes)))
                pending.removeFirst(Self.chunkBytes)
            }
            return chunks
        }
    }

    /// Returns the leftover partial chunk (< 100 ms), if any.
    func flush() -> Data? {
        lock.withLock {
            defer { pending = Data() }
            return pending.isEmpty ? nil : pending
        }
    }

    private func convert(_ buffer: AVAudioPCMBuffer) -> Data {
        let ratio = Self.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount((Double(buffer.frameLength) * ratio).rounded(.up)) + 64
        guard
            let output = AVAudioPCMBuffer(pcmFormat: Self.outputFormat, frameCapacity: capacity)
        else { return Data() }

        var supplied = false
        var error: NSError?
        let status = converter.convert(to: output, error: &error) { _, inputStatus in
            // Hand over this one buffer, then report "no data *yet*" (not end-of-stream)
            // so the resampler keeps its state for the next tap callback.
            if supplied {
                inputStatus.pointee = .noDataNow
                return nil
            }
            supplied = true
            inputStatus.pointee = .haveData
            return buffer
        }
        guard status != .error, let samples = output.int16ChannelData else { return Data() }
        return Data(bytes: samples[0], count: Int(output.frameLength) * MemoryLayout<Int16>.size)
    }
}
