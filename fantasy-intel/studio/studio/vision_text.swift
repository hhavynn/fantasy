// Text found in images by Apple's Vision framework, on device. Prints JSON: {"<path>": ["line", ...], ...}.
// Used by the AI parody QC to find the badge in rendered frames and stray text in generated pictures.
// Usage: swift vision_text.swift <image> [<image> ...]
import Foundation
import Vision

var out: [String: [String]] = [:]
for path in CommandLine.arguments.dropFirst() {
    let handler = VNImageRequestHandler(url: URL(fileURLWithPath: path), options: [:])
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    try handler.perform([request])
    out[path] = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
}
let data = try JSONSerialization.data(withJSONObject: out)
print(String(data: data, encoding: .utf8)!)
