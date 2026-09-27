@preconcurrency import AVFoundation

/// Live microphone capture via AVAudioEngine, resampled to 16 kHz mono PCM16.
public final class MicrophoneCapture: AudioCapturing, @unchecked Sendable {
    private let lock = NSLock()
    private var engine: AVAudioEngine?  // guarded by lock
    private var chunker: PCMChunker?  // guarded by lock
    private var continuation: AsyncStream<Data>.Continuation?  // guarded by lock

    public init() {}

    public func requestPermission() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            return true
        case .notDetermined:
            return await AVCaptureDevice.requestAccess(for: .audio)
        default:
            return false
        }
    }

    public func start() throws -> AsyncStream<Data> {
        stop()
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw AudioCaptureError.noInputDevice
        }
        let chunker = try PCMChunker(inputFormat: format)
        let (stream, continuation) = AsyncStream.makeStream(
            of: Data.self, bufferingPolicy: .bufferingNewest(600))

        // ~100 ms per callback at 48 kHz; runs on a real-time audio thread.
        input.installTap(onBus: 0, bufferSize: 4_800, format: format) { buffer, _ in
            for chunk in chunker.append(buffer) {
                continuation.yield(chunk)
            }
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            continuation.finish()
            throw AudioCaptureError.engineFailed(error.localizedDescription)
        }
        lock.withLock {
            self.engine = engine
            self.chunker = chunker
            self.continuation = continuation
        }
        return stream
    }

    public func stop() {
        let (engine, chunker, continuation) = lock.withLock {
            defer {
                self.engine = nil
                self.chunker = nil
                self.continuation = nil
            }
            return (self.engine, self.chunker, self.continuation)
        }
        engine?.inputNode.removeTap(onBus: 0)
        engine?.stop()
        // Send the last partial chunk so the tail of the utterance isn't lost.
        if let tail = chunker?.flush() { continuation?.yield(tail) }
        continuation?.finish()
    }
}
