# XLogic — Sunum konuşması (4 slayt)

**Süre hedefi:** ~2 dk 30 sn (sıkı: 2 dk · rahat: 3 dk)  
**Repo:** https://github.com/sirius-labs-dev/xlogic  
**Kontrol:** ←→ / space · F fullscreen

---

## Slayt 1 — Açılış (~25 sn)

Merhaba, ben [adın]. Projem **XLogic**.

Bugün üç şey göstereceğim: **problem, çözüm, mimari**.

XLogic, OKX TR’de çalışan **fail-closed** bir otonom spot ajanı.  
Yani ajan “al” dese bile — emir **risk kapısından** geçmeden borsaya gitmiyor.

Canlı koştuk: ~30 dolarlık hesap, yüzlerce cycle, gerçek fill’ler, kapı reddetmeleri de logda.

---

## Slayt 2 — Problem (~35 sn)

Soru şu: **AI “al” dediğinde kim fren basıyor?**

LLM genelde agresif önerir. Borsa da “tamam” der — boyut, stop, günlük zarar bakmaz.

Chat bot ≠ otonomi. Prompt’a “dikkatli ol” yazmak risk yönetimi değil.

Stop yok, boyut yok, limit yoksa — bu ajan değil, **kumar**.

Hackathon’da istediğimiz şey: karar verebilen ama **kendi kendini frenleyen** sistem.

---

## Slayt 3 — Çözüm (~40 sn)

Bizim cümlemiz: **Karar özgür. İcra kapılı.**

Sinyaller ve LLM — ya da LLM çökerse rules — **karar** üretir.  
Ama borsaya yalnız **`risk_gate` izin verirse** emir çıkar.

Kapı reddederse logda **GATED** görürsünüz — emir yok.  
LLM timeout / bozuk schema olursa **`rules_fallback`** ya da **HOLD**.  
Asla “yine de bas” yok. Fail-closed.

Canlıda da gördük: `BELOW_MIN_SIZE`, `OVER_POSITION`… ajan al demiş, kapı demiş hayır.

---

## Slayt 4 — Mimari + kapanış (~50 sn)

Akış beş adım:

1. **OBSERVE** — tüm OKX çağrıları `okx --json` CLI. İmza yazmadık; ATK uyumlu.  
2. **SIGNALS** — EMA, ATR, entry/exit.  
3. **DECIDE** — LLM auto, fallback rules.  
4. **GATE** — boyut + 13 red kodu + kill-switch. Burası kırmızı çizgi.  
5. **ORDER** — limit + stop/TP **borsada ilişik**, journal’a yazılır.

Cutoff 19:15 yeni alım yok, 19:20 hard stop — bugün ajan planlı kapandı.

Özet: otonomi var, ama icra her zaman kapıdan geçiyor.  
Kod, metrik, konsol ekranı ve demo video GitHub’da: **sirius-labs-dev/xlogic**.

Teşekkürler — sorularınızı alayım.

---

## Kısa yedek (≤60 sn — jüri sıkıştırırsa)

XLogic: OKX TR spot’ta fail-closed otonom ajan.  
LLM karar verir, **risk_gate** icra eder; gate hayır derse emir yok.  
Tüm borsa işi `okx --json` CLI — ATK. Stop emre ilişik.  
Canlı: ~30$ hesap, 4 fill, GATED frenleri, 40 test.  
Repo: github.com/sirius-labs-dev/xlogic.

---

## Demo / soru için hazır cevaplar

| Soru | Cevap (1 cümle) |
|------|------------------|
| LLM çökerse? | rules_fallback veya HOLD — blind order yok. |
| Neden CLI? | ATK: imza yok, `okx --json`, resmi tool yüzeyi. |
| Risk nerede? | %0.5/trade, %30 pos, %60 exposure, %2 daily kill, rate limit, cutoff. |
| Kanıt? | clOrdId `agt…`, journal, performance.md, live console SS. |
| PnL? | Day start ~$30 → ~$29.95; amaç güvenli otonomi, sadece PnL değil. |
| Flatten fail? | Cutoff’ta bir ETH flatten fail loglandı; hard stop sonrası manuel kontrol. |

---

## Prova notları

- Slayt başına **bir** ana cümle; madde madde okuma.  
- “Kapılı / GATED / fail-closed” üç kez geçsin — jüri kriteri.  
- Repo linkini **son** söyle; ortada sayma.  
- Heyecan: problemde yavaş, çözümde net, mimaride hızlı.

---

## Demo anlatımı — hikâye (basit dil) (~60–90 sn)

*Videoyu / konsolu açınca bunu söyle. Teknik jargon yok; bir günün hikâyesi.*

---

Sabah küçük bir hesabı açtık. Yaklaşık **otuz dolar**.  
Sonra ajanı bıraktık: bak, düşün, karar ver — ama her şeyi borsaya basma.

Ekranda gördüğünüz şey o günün canlı panosu.  
Her dakika civarı bir tur atıyor: fiyatı okuyor, sinyale bakıyor, “alayım mı?” diye soruyor.

Çoğu zaman cevap **bekle**. Çünkü piyasa uygun değilse zorla girmiyoruz.  
Bazen “al” diyor. O anda hemen emir gitmiyor. Önce bir **kapı** var.

Kapı diyor ki: boyut küçük mü? Pozisyon dolu mu? Bugün yeterince zarar mı ettik?  
Eğer hayır — ekranda kırmızı bir fren görüyorsunuz: **GATED**. Emir yok. Para güvende.

Birkaç kez kapı açıldı. Gerçekten ethereum aldık. Sonra sattık. Sonra tekrar girdik.  
Stop’lar da emrin yanında — ajan düşerse bile borsa korumayı bırakmıyor.

Öğleden sonra yine “al” dedi; kapı bazen “çok küçük” veya “zaten pozisyon var” deyip kesti.  
Bu bizim için başarı: ajan çalışıyor ama **dizgin elimizde**.

Akşama doğru yeni alım kapandı, sonra ajan planlı durdu.  
Hesap hâlâ ~30 dolar civarı. Büyük jackpot yok — ama kör emir de yok.

Kısaca hikâye şu: **yapay zekâ konuşur, kapı karar verir, borsa sadece izin verileni alır.**

---

### Daha da kısa versiyon (~40 sn)

Sabah 30 dolarla başladık. Ajan her turda bakıyor, düşünüyor.  
“Al” dese bile önce kapıdan geçiyor; kapı hayır derse emir yok — ekranda GATED.  
Birkaç gerçek alım-satım yaptık, stop’lar borsada.  
Akşam ajan kendi kendine kapandı. Sonuç: otonomi var, kontrolden çıkma yok.
