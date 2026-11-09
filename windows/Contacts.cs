using System;
using System.Collections.Generic;
using System.Drawing;
using System.Linq;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows.Forms;

namespace GaevAssistant {
 sealed class ContactCard {
  public string Id="",Name="",Category="",Body="",Url="",Email="";public List<string> Phones=new List<string>();
  static string Clean(string text) {return Regex.Replace(WebUtility.HtmlDecode(Regex.Replace(text,@"<[^>]+>"," ")),@"\s+"," ").Trim();}
  static string JsString(string value) {
   var b=new StringBuilder("\"");foreach(var c in value) {switch(c){case '"':b.Append("\\\"");break;case '\\':b.Append("\\\\");break;case '\b':b.Append("\\b");break;case '\f':b.Append("\\f");break;case '\n':b.Append("\\n");break;case '\r':b.Append("\\r");break;case '\t':b.Append("\\t");break;default:if(c<32)b.Append("\\u"+((int)c).ToString("x4"));else b.Append(c);break;}}return b.Append('"').ToString();
  }
  public static string Key(string url,string name,List<string> phones) {
   var values=new List<string>();if(!String.IsNullOrEmpty(url)){values.Add("public");values.Add(url);}else{values.Add(name);values.AddRange(phones.Select(p=>"tel:"+p).OrderBy(p=>p,StringComparer.Ordinal));}
   var raw="["+String.Join(",",values.Select(JsString))+"]";using(var hash=SHA256.Create())return String.Concat(hash.ComputeHash(Encoding.UTF8.GetBytes(raw)).Select(x=>x.ToString("x2")));
  }
  public static List<ContactCard> Read(string page) {
   var cards=new List<ContactCard>();
   foreach(var section in new[]{"new-business-contacts","business-contacts"}) {
    int start=page.IndexOf("<section id=\""+section+"\"",StringComparison.Ordinal);if(start<0)continue;int end=page.IndexOf("</section>",start,StringComparison.Ordinal);if(end<0)continue;var part=page.Substring(start,end-start);
    foreach(Match m in Regex.Matches(part,@"<details(?<attrs>[^>]*)>\s*<summary>(?<name>.*?)</summary>(?<body>.*?)</details>",RegexOptions.Singleline|RegexOptions.IgnoreCase)) {
     var card=new ContactCard();var lead=Regex.Match(m.Groups["attrs"].Value,@"data-lead=""(?<url>[^""]+)""");card.Url=lead.Success?WebUtility.HtmlDecode(lead.Groups["url"].Value):"";
     card.Name=Clean(m.Groups["name"].Value);if(card.Name=="")continue;var body=m.Groups["body"].Value;card.Body=Clean(body);
     foreach(Match a in Regex.Matches(body,@"href=""tel:(?<v>[^""]+)""",RegexOptions.IgnoreCase))card.Phones.Add(WebUtility.HtmlDecode(a.Groups["v"].Value));
     var em=Regex.Match(body,@"href=""mailto:(?<v>[^""]+)""",RegexOptions.IgnoreCase);card.Email=em.Success?WebUtility.HtmlDecode(em.Groups["v"].Value):"";
     var headings=Regex.Matches(part.Substring(0,m.Index),@"<h3>(?<h>.*?)</h3>",RegexOptions.Singleline|RegexOptions.IgnoreCase);card.Category=headings.Count>0?Clean(headings[headings.Count-1].Groups["h"].Value):"Контакты";
     var originalName=WebUtility.HtmlDecode(Regex.Replace(m.Groups["name"].Value,@"<[^>]+>",""));
     card.Id=Key(card.Url,originalName,card.Phones);cards.Add(card);
    }
   }
   return cards;
  }
 }

}
