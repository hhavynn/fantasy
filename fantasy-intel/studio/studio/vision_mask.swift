// The foreground subject of an image as a grayscale PNG mask, from Apple's Vision framework, on device (macOS 14+).
// Exits 2 when Vision finds no subject. Usage: swift vision_mask.swift <image> <mask.png>
import CoreImage
import Foundation
import Vision

let input = URL(fileURLWithPath: CommandLine.arguments[1])
let output = URL(fileURLWithPath: CommandLine.arguments[2])
let handler = VNImageRequestHandler(url: input, options: [:])
let request = VNGenerateForegroundInstanceMaskRequest()
try handler.perform([request])
guard let result = request.results?.first, !result.allInstances.isEmpty else { exit(2) }
let buffer = try result.generateScaledMaskForImage(forInstances: result.allInstances, from: handler)
let gray = CGColorSpace(name: CGColorSpace.linearGray)!
try CIContext().writePNGRepresentation(of: CIImage(cvPixelBuffer: buffer), to: output, format: .L8, colorSpace: gray)
