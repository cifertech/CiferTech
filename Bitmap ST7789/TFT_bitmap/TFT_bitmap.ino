#include <TFT_eSPI.h>
#include "bitmap.h" 

TFT_eSPI tft = TFT_eSPI(); 

void setup(void) {
  Serial.begin(115200);
  Serial.print("TFT Test");

  tft.begin();     
  tft.setSwapBytes(true); 

  tft.fillScreen(TFT_BLACK);
  tft.pushImage(0,0,240,240,mercy);
}

void loop() {
}
