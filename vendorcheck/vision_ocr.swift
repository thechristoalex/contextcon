// Reads printed text from a bill photo with Apple Vision, on this machine (no network, no cost).
// Compiled once by vendorcheck/scan.py into data/bin/vision_ocr.
//
//   vision_ocr <image>   -> {"size": [w, h], "lines": [{"text", "conf", "box": [x, y, w, h], "quad": [8 numbers]}]}
//                           box and quad are 0..1 with y from the top; quad is the line's corners
//                           (top-left, top-right, bottom-right, bottom-left), which give the page's tilt
//   vision_ocr --warm    -> loads the model on a blank image and prints {"size": [64, 64], "lines": []}
import CoreGraphics
import Foundation
import ImageIO
import Vision

func read(_ handler: VNImageRequestHandler) throws -> [[String: Any]] {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = false  // keep codes, GSTINs and amounts exactly as printed
    try handler.perform([req])
    return (req.results ?? []).compactMap { o in
        guard let c = o.topCandidates(1).first else { return nil }
        let b = o.boundingBox
        let corners = [o.topLeft, o.topRight, o.bottomRight, o.bottomLeft].flatMap { [Double($0.x), Double(1 - $0.y)] }
        return ["text": c.string, "conf": Double(c.confidence),
                "box": [Double(b.minX), Double(1 - b.maxY), Double(b.width), Double(b.height)], "quad": corners]
    }
}

/// Pixel size as Vision sees the image (EXIF orientations 5-8 are rotated a quarter turn).
func pixelSize(_ url: URL) -> [Int] {
    guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
          let p = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any],
          let w = p[kCGImagePropertyPixelWidth] as? Int, let h = p[kCGImagePropertyPixelHeight] as? Int else { return [1, 1] }
    let o = p[kCGImagePropertyOrientation] as? Int ?? 1
    return o >= 5 ? [h, w] : [w, h]
}

let args = CommandLine.arguments
guard args.count > 1 else {
    FileHandle.standardError.write("usage: vision_ocr <image> | --warm\n".data(using: .utf8)!)
    exit(2)
}
let handler: VNImageRequestHandler
var size = [64, 64]
if args[1] == "--warm" {
    let ctx = CGContext(data: nil, width: 64, height: 64, bitsPerComponent: 8, bytesPerRow: 0,
                        space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue)!
    ctx.setFillColor(gray: 1, alpha: 1)
    ctx.fill(CGRect(x: 0, y: 0, width: 64, height: 64))
    handler = VNImageRequestHandler(cgImage: ctx.makeImage()!)
} else {
    let url = URL(fileURLWithPath: args[1])
    size = pixelSize(url)
    handler = VNImageRequestHandler(url: url)
}
do {
    let out: [String: Any] = ["size": size, "lines": try read(handler)]
    FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: out))
} catch {
    FileHandle.standardError.write("\(error)\n".data(using: .utf8)!)
    exit(1)
}
