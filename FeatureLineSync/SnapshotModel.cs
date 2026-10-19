using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace FeatureLineSync
{
    /// <summary>
    /// Everything the planner needs to know about the feature lines of one drawing.
    /// Produced by FeatureLineReader (host document, side database or open document),
    /// written by FLEXPORT and the pre-apply backup, read back by FLIMPORT.
    /// No AutoCAD types: the same model is compiled into the unit tests.
    /// </summary>
    public sealed class FlSnapshot
    {
        public const int CurrentSchema = 1;

        public int Schema { get; set; } = CurrentSchema;
        public string Source { get; set; } = "";
        public DateTime SavedAt { get; set; } = DateTime.Now;
        public List<FlLine> Lines { get; set; } = new();
        /// <summary>Lines that were seen but not captured (dynamic, reference, read error) with the reason.</summary>
        public List<string> Skipped { get; set; } = new();

        public static readonly JsonSerializerOptions JsonOptions = new()
        {
            WriteIndented = true,
            PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
            DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        };

        public string ToJson() => JsonSerializer.Serialize(this, JsonOptions);

        public static FlSnapshot FromJson(string json)
        {
            FlSnapshot? s = JsonSerializer.Deserialize<FlSnapshot>(json, JsonOptions);
            if (s == null) throw new InvalidOperationException("Snapshot file is empty or not JSON.");
            if (s.Schema != CurrentSchema)
                throw new InvalidOperationException($"Snapshot schema {s.Schema} is not supported (expected {CurrentSchema}).");
            return s;
        }
    }

    public sealed class FlLine
    {
        public string Handle { get; set; } = "";
        public string Name { get; set; } = "";
        public string Site { get; set; } = "";
        public string Style { get; set; } = "";
        public string Layer { get; set; } = "";
        public string RxClass { get; set; } = "";
        public bool Closed { get; set; }
        public bool LayerLocked { get; set; }

        /// <summary>PI points in order: [x, y, z].</summary>
        public List<double[]> Pis { get; set; } = new();
        /// <summary>Bulge per PI segment: Pis.Count-1 entries for open lines, Pis.Count for closed ones.</summary>
        public List<double> Bulges { get; set; } = new();
        /// <summary>Elevation points in order along the line, located on the PI polyline.</summary>
        public List<FlElevPoint> ElevationPoints { get; set; } = new();
        /// <summary>2D length of each PI segment (same count as Bulges).</summary>
        public List<double> SegLengths2D { get; set; } = new();
        public double Length2D { get; set; }
        public double Length3D { get; set; }

        [JsonIgnore] public int SegmentCount => Closed ? Pis.Count : Math.Max(0, Pis.Count - 1);
    }

    public sealed class FlElevPoint
    {
        public double X { get; set; }
        public double Y { get; set; }
        public double Z { get; set; }
        /// <summary>Index of the PI segment the point lies on.</summary>
        public int Segment { get; set; }
        /// <summary>Position along that segment's 2D length, 0 at the segment start, 1 at its end.</summary>
        public double Fraction { get; set; }
    }
}
