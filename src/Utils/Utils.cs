using System.Globalization;
using System.Text;

namespace Utils;

public static class TextTools
{
    /// <summary>
    /// Returns true if the input is a palindrome.
    /// Rules:
    /// - null => false, empty => true
    /// - Ignores case and non-alphanumeric characters
    /// - Unicode-aware (letters/digits via Unicode categories; compares by Rune)
    /// </summary>
    public static bool IsPalindrome(string? input)
    {
        if (input is null) return false;
        if (input.Length == 0) return true;

        // Normalize, keep only letters/digits, lowercase using Unicode rules.
        
        var normalized = input.Normalize(NormalizationForm.FormC);
        var runes = new List<Rune>();

        foreach (var r in normalized.EnumerateRunes())
        {
            var cat = Rune.GetUnicodeCategory(r);
            var isLetterOrDigit =
                cat == UnicodeCategory.DecimalDigitNumber ||
                cat == UnicodeCategory.LowercaseLetter   ||
                cat == UnicodeCategory.UppercaseLetter   ||
                cat == UnicodeCategory.TitlecaseLetter   ||
                cat == UnicodeCategory.OtherLetter;

            if (isLetterOrDigit)
                runes.Add(Rune.ToLowerInvariant(r));
        }

        int i = 0, j = runes.Count - 1;
        while (i < j)
        {
            if (!runes[i].Equals(runes[j])) return false;
            i++; j--;
        }
        return true;
    }
}
