import Foundation

/// Microphone input as 16 kHz mono PCM16 little-endian chunks.
public protocol AudioCapturing: Sendable {
    /// Returns whether mic access is granted, prompting the user the first time.
    func requestPermission() async -> Bool
    /// Starts capturing. The stream finishes after `stop()`.
    func start() throws -> AsyncStream<Data>
    func stop()
}

public enum AudioCaptureError: Error, Equatable, LocalizedError {
    case noInputDevice
    case unsupportedFormat(String)
    case engineFailed(String)

    public var errorDescription: String? {
        switch self {
        case .noInputDevice: return "No microphone is available."
        case .unsupportedFormat(let format): return "Unsupported microphone format: \(format)"
        case .engineFailed(let message): return "Couldn't start the microphone: \(message)"
        }
    }
}
