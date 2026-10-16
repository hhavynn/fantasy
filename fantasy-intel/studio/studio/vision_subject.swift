// Subject boxes for the media catalog (studio/media_sources.py): faces and people found by Apple's Vision
// framework, on device. Prints JSON with boxes as [left, top, right, bottom] fractions of the image (top-left
// origin), largest first. Usage: swift vision_subject.swift <image>
import Foundation
import Vision

let url = URL(fileURLWithPath: CommandLine.arguments[1])
let handler = VNImageRequestHandler(url: url, options: [:])
let faces = VNDetectFaceRectanglesRequest()
let humans = VNDetectHumanRectanglesRequest()
humans.upperBodyOnly = false
try handler.perform([faces, humans])

func boxes(_ obs: [VNDetectedObjectObservation]?) -> [[Double]] {
    let bs = (obs ?? []).map { o -> [Double] in
        let r = o.boundingBox
        return [Double(r.minX), Double(1 - r.maxY), Double(r.maxX), Double(1 - r.minY), Double(o.confidence)]
    }
    return bs.sorted { ($0[2] - $0[0]) * ($0[3] - $0[1]) > ($1[2] - $1[0]) * ($1[3] - $1[1]) }
}

let out: [String: Any] = ["faces": boxes(faces.results), "humans": boxes(humans.results)]
let data = try JSONSerialization.data(withJSONObject: out)
print(String(data: data, encoding: .utf8)!)
