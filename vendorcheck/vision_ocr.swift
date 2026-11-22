// Reads printed text from a bill photo with Apple Vision, on this machine (no network, no cost).
// Compiled once by vendorcheck/scan.py into data/bin/vision_ocr.
//
//   vision_ocr <image>   -> JSON array of {"text", "conf", "box": [x, y, w, h]} (box 0..1, y from the top)
//   vision_ocr --warm    -> loads the model on a blank image and prints []
import CoreGraphics
import Foundation
import Vision

func read(_ handler: VNImageRequestHandler) throws -> [[String: Any]] {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = false  // keep codes, GSTINs and amounts exactly as printed
    try handler.perform([req])
    return (req.results ?? []).compactMap { o in
        guard let c = o.topCandidates(1).first else { return nil }
        let b = o.boundingBox
        return ["text": c.string, "conf": Double(c.confidence),
                "box": [Double(b.minX), Double(1 - b.maxY), Double(b.width), Double(b.height)]]
    }
}

let args = CommandLine.arguments
guard args.count > 1 else {
    FileHandle.standardError.write("usage: vision_ocr <image> | --warm\n".data(using: .utf8)!)
    exit(2)
}
let handler: VNImageRequestHandler
if args[1] == "--warm" {
    let ctx = CGContext(data: nil, width: 64, height: 64, bitsPerComponent: 8, bytesPerRow: 0,
                        space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue)!
    ctx.setFillColor(gray: 1, alpha: 1)
    ctx.fill(CGRect(x: 0, y: 0, width: 64, height: 64))
    handler = VNImageRequestHandler(cgImage: ctx.makeImage()!)
} else {
    handler = VNImageRequestHandler(url: URL(fileURLWithPath: args[1]))
}
do {
    FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: try read(handler)))
} catch {
    FileHandle.standardError.write("\(error)\n".data(using: .utf8)!)
    exit(1)
}
