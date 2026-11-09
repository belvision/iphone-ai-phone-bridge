using System.Net.Http.Json;
using System.Text.Json.Nodes;

namespace GaevAssistant;
sealed partial class MainWindow {
 const string Secretary="ИИ-секретарь", Market="Менеджер маркетплейса", Accounts="Бухгалтер";
 static string Offer(string offer,string example,string callback) => $"""
 Ты — помощница программиста Сергея Гаева. Говори по-русски, коротко и в женском роде.
 Во время автоответчика, музыки или голосового меню молчи, используй skip_turn. Не принимай тишину за ответ.
 После живого «Алло», «Да», «Слушаю» или представления сотрудника сразу скажи одной репликой: «Здравствуйте! Я помощница программиста Сергея Гаева. Вам удобно сейчас говорить?»
 Если удобно, спроси: «{offer}» Дождись ответа. Согласие говорить само по себе не означает интерес.
 Если просят пояснить, скажи: «{example}» Состав работ, стоимость, сроки и доступы обсуждает Сергей. Не выдумывай гарантии или квалификацию.
 Если неудобно, спроси: «Когда Сергею удобно вам перезвонить насчёт {callback}?» Повтори время и попрощайся.
 Если заинтересованы, спроси: «Сергей Гаев может перезвонить вам через минуту и объяснить детали. Вам удобно?» При согласии поблагодари и попрощайся. Если называют другое время, повтори его.
 При отказе скажи: «Поняла, спасибо. До свидания!» Если просят подождать, молчи. Не говори «Извините, вас плохо слышно». После прощания вызови end_call.
 """;
 void BuildScenarios(){
  var page=Page("Сценарии");
  JsonObject cfg;
  try{cfg=Data.Secrets("eleven.dpapi");}catch(FileNotFoundException){cfg=new();}
  var profiles=cfg["scenarios"]?.DeepClone().AsObject()??new JsonObject();
  profiles[Secretary]??=Data.S(cfg,"prompt");
  profiles[Market]??=Offer("Вам интересны услуги менеджера для вашего магазина на маркетплейсе?","Менеджер может вести карточки товаров, следить за остатками и ценами и готовить отчёты по продажам. Что из этого отнимает у вас больше всего времени?","ведения магазина на маркетплейсе");
  profiles[Accounts]??=Offer("Вам интересны услуги бухгалтера для вашего бизнеса?","Можно обсудить работу с первичными документами, сверки с контрагентами и подготовку отчётности. С какой задачей вам нужна помощь?","бухгалтерского обслуживания");
  var layout=new TableLayoutPanel{Dock=DockStyle.Fill,RowCount=3,ColumnCount=1};
  layout.RowStyles.Add(new(SizeType.Absolute,42));layout.RowStyles.Add(new(SizeType.Percent,100));layout.RowStyles.Add(new(SizeType.Absolute,55));
  page.Controls.Add(layout);
  var picker=new ComboBox{Dock=DockStyle.Fill,DropDownStyle=ComboBoxStyle.DropDownList};
  picker.Items.AddRange(new[]{Secretary,Market,Accounts});
  var editor=new TextBox{Multiline=true,Dock=DockStyle.Fill,ScrollBars=ScrollBars.Vertical,AcceptsReturn=true};
  var actions=new FlowLayoutPanel{Dock=DockStyle.Fill};
  var apply=new Button();Style(apply,"Применить в ElevenLabs");
  string active=Data.S(cfg,"activeScenario");if(active=="")active=Secretary;
  var status=new Label{Text="Для звонков: "+active,AutoSize=true,Padding=new(4,8,0,0)};
  actions.Controls.Add(apply);actions.Controls.Add(status);layout.Controls.Add(picker,0,0);layout.Controls.Add(editor,0,1);layout.Controls.Add(actions,0,2);
  string selectedProfile="";
  picker.SelectedIndexChanged+=(s,e)=>{
   if(selectedProfile!="")profiles[selectedProfile]=editor.Text;
   selectedProfile=picker.Text;editor.Text=profiles[selectedProfile]?.GetValue<string>()??"";
  };
  picker.SelectedItem=active;
  apply.Click+=async(s,e)=>{
   var prompt=editor.Text.Trim();if(prompt==""){MessageBox.Show("Введите текст сценария.");return;}
   var chosen=picker.Text;profiles[chosen]=prompt;
   Enabled=false;
   try{
    var current=Data.Secrets("eleven.dpapi");
    using var http=Data.Http();
    var url="https://api.elevenlabs.io/v1/convai/agents/"+Uri.EscapeDataString(Data.S(current,"agentId"));
    var content=new {conversation_config=new {agent=new {prompt=new {prompt}}}};
    using var response=await http.PatchAsync(url,JsonContent.Create(content));
    if(!response.IsSuccessStatusCode)throw new Exception("ElevenLabs вернул HTTP "+(int)response.StatusCode+". Проверьте права ключа на редактирование агента.");
    var actual=JsonNode.Parse(await http.GetStringAsync(url));
    if(actual?["conversation_config"]?["agent"]?["prompt"]?["prompt"]?.GetValue<string>()!=prompt)throw new Exception("ElevenLabs не подтвердил сохранённый текст.");
    current["scenarios"]=profiles.DeepClone();current["activeScenario"]=chosen;current["prompt"]=prompt;
    Data.SaveSecrets("eleven.dpapi",current);status.Text="Для звонков: "+chosen;
   }catch(Exception ex){MessageBox.Show("Не удалось подтвердить применение сценария. "+ex.Message);}
   finally{Enabled=true;}
  };
 }
}
