using System;
using System.Collections.Generic;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.Civil.ApplicationServices;
using CivilSurface = Autodesk.Civil.DatabaseServices.Surface;

namespace FeatureLineSync
{
    /// <summary>
    /// "Does this point touch the corridor?" = does the chosen surface have an elevation there.
    /// FindElevationAtXY throws outside the surface boundary; a ring probe at the tolerance
    /// radius catches points that sit just outside the edge of the corridor surface.
    /// </summary>
    public sealed class SurfaceProbe
    {
        private readonly CivilSurface _surface;
        private readonly double _radius;

        public SurfaceProbe(CivilSurface surface, double ringRadius)
        {
            _surface = surface;
            _radius = Math.Max(0, ringRadius);
        }

        public double? Probe(double x, double y)
        {
            if (TryAt(x, y, out double z)) return z;
            if (_radius <= 0) return null;
            foreach (double r in new[] { _radius * 0.5, _radius })
            {
                for (int k = 0; k < 8; k++)
                {
                    double a = k * Math.PI / 4.0;
                    if (TryAt(x + r * Math.Cos(a), y + r * Math.Sin(a), out z)) return z;
                }
            }
            return null;
        }

        /// <summary>Diagnostic: the smallest ring radius (from a fixed ladder) at which the surface has coverage near (x, y), or null.</summary>
        public double? NearestCoverage(double x, double y, double maxRadius)
        {
            if (TryAt(x, y, out _)) return 0;
            foreach (double r in new[] { 0.5, 1, 2, 5, 10, 25, 50 })
            {
                if (r > maxRadius) break;
                for (int k = 0; k < 16; k++)
                {
                    double a = k * Math.PI / 8.0;
                    if (TryAt(x + r * Math.Cos(a), y + r * Math.Sin(a), out _)) return r;
                }
            }
            return null;
        }

        private bool TryAt(double x, double y, out double z)
        {
            try
            {
                z = _surface.FindElevationAtXY(x, y);
                return !double.IsNaN(z) && !double.IsInfinity(z);
            }
            catch
            {
                z = 0;
                return false;
            }
        }

        /// <summary>All surfaces of the active Civil 3D document, in the order Civil 3D lists them.</summary>
        public static List<(string name, ObjectId id)> ListSurfaces(Transaction tr)
        {
            var result = new List<(string, ObjectId)>();
            CivilDocument civil = CivilApplication.ActiveDocument;
            foreach (ObjectId id in civil.GetSurfaceIds())
            {
                if (tr.GetObject(id, OpenMode.ForRead, false) is CivilSurface s)
                    result.Add((s.Name, id));
            }
            return result;
        }
    }
}
