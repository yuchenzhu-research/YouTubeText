import Foundation
import ImageIO
import Vision

private struct OCRRequest: Decodable {
    let images: [String]
    let languages: [String]
    let accurate: Bool
    let minimumTextHeight: Double

    private enum CodingKeys: String, CodingKey {
        case images
        case languages
        case accurate
        case minimumTextHeight
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        images = try values.decode([String].self, forKey: .images)
        languages = try values.decodeIfPresent([String].self, forKey: .languages) ?? []
        accurate = try values.decodeIfPresent(Bool.self, forKey: .accurate) ?? true
        minimumTextHeight = try values.decodeIfPresent(
            Double.self,
            forKey: .minimumTextHeight
        ) ?? 0.012
    }
}

private struct BoundingBox: Encodable {
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

private struct TextObservation: Encodable {
    let text: String
    let confidence: Float
    let boundingBox: BoundingBox
}

private struct FrameResult: Encodable {
    let path: String
    let observations: [TextObservation]
    let error: String?
}

private struct OCRResponse: Encodable {
    let engine = "apple-vision"
    let frames: [FrameResult]
}

private struct ErrorResponse: Encodable {
    let error: String
}

private enum CommandError: LocalizedError {
    case invalidRequest(String)
    case unreadableImage(String)

    var errorDescription: String? {
        switch self {
        case .invalidRequest(let message):
            return message
        case .unreadableImage(let path):
            return "Unable to read image: \(path)"
        }
    }
}

private func writeJSON<T: Encodable>(_ value: T, to handle: FileHandle) throws {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.withoutEscapingSlashes]
    handle.write(try encoder.encode(value))
    handle.write(Data([0x0A]))
}

private func loadImage(at path: String) throws -> CGImage {
    let url = URL(fileURLWithPath: path)
    guard
        let source = CGImageSourceCreateWithURL(url as CFURL, nil),
        let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
    else {
        throw CommandError.unreadableImage(path)
    }
    return image
}

private func recognize(path: String, request input: OCRRequest) -> FrameResult {
    do {
        let image = try loadImage(at: path)
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = input.accurate ? .accurate : .fast
        request.usesLanguageCorrection = true
        request.minimumTextHeight = Float(max(0.0, min(1.0, input.minimumTextHeight)))

        if !input.languages.isEmpty {
            let supportedLanguages = try request.supportedRecognitionLanguages()
            let requestedLanguages = input.languages.filter(supportedLanguages.contains)
            if !requestedLanguages.isEmpty {
                request.recognitionLanguages = requestedLanguages
            }
        }

        try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])

        let observations = (request.results ?? []).compactMap { observation -> TextObservation? in
            guard let candidate = observation.topCandidates(1).first else {
                return nil
            }
            let box = observation.boundingBox
            return TextObservation(
                text: candidate.string,
                confidence: candidate.confidence,
                boundingBox: BoundingBox(
                    x: box.origin.x,
                    y: box.origin.y,
                    width: box.size.width,
                    height: box.size.height
                )
            )
        }.sorted { left, right in
            let rowDifference = left.boundingBox.y - right.boundingBox.y
            if abs(rowDifference) > 0.025 {
                return left.boundingBox.y > right.boundingBox.y
            }
            return left.boundingBox.x < right.boundingBox.x
        }

        return FrameResult(path: path, observations: observations, error: nil)
    } catch {
        return FrameResult(path: path, observations: [], error: error.localizedDescription)
    }
}

@main
private struct VisionOCRCommand {
    static func main() {
        do {
            let data = FileHandle.standardInput.readDataToEndOfFile()
            guard !data.isEmpty else {
                throw CommandError.invalidRequest("Expected a JSON request on stdin")
            }

            let input = try JSONDecoder().decode(OCRRequest.self, from: data)
            guard !input.images.isEmpty else {
                throw CommandError.invalidRequest("The images array must not be empty")
            }

            var frames: [FrameResult] = []
            frames.reserveCapacity(input.images.count)
            for path in input.images {
                autoreleasepool {
                    frames.append(recognize(path: path, request: input))
                }
            }

            try writeJSON(OCRResponse(frames: frames), to: .standardOutput)
        } catch {
            try? writeJSON(ErrorResponse(error: error.localizedDescription), to: .standardError)
            Foundation.exit(EXIT_FAILURE)
        }
    }
}
