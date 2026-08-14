/*
 * IncFile1.h
 *
 * Created: 25/02/2016 8:33:56
 *  Author: agarcia
 */ 


#ifndef INCFILE1_H_
#define INCFILE1_H_

#include <stdint.h>
#include <stddef.h>

#define LEDS_NUMBER 3
#define BLINK_FOREVER	0x100

#define MODE_LED_MASK  ((1 << eLED_MODE_O) | (1 << eLED_MODE_G))
#define BATT_LED_MASK  ((1 << eLED_BATT_G) | (1 << eLED_BATT_O) | (1 << eLED_BATT_R))

typedef struct
{
	union
	{
		struct
		{
			uint32_t Counter:8;
			uint32_t nIter:8;
			uint32_t:8;
			uint32_t LedState:1;
			uint32_t LedOut:1;
			uint32_t Loop:1;
			uint32_t InvertOut:1;
			uint32_t Enable:1;
			uint32_t :3;
		}bits;
		uint32_t reg;
	};
	void *port;
	uint32_t pin;
	void (*out_led)(void *, uint32_t);	//Led out function pointer, argument passed is stLedConfig * pointer and output value
	uint16_t On_Time;	// On time in ms
	uint16_t Off_Time;	// Off time in ms
	uint32_t StartTime;
}stLedConfig;

void BlinkLed_Get_Default(stLedConfig *);
void BlinkLed_Set_Period(stLedConfig *, uint32_t, uint32_t);
void BlinkLed_Idx_Start(uint32_t , uint32_t);
void BlinkLed_Start(stLedConfig *, uint32_t);
void BlinkLed_Stop(stLedConfig *);
stLedConfig* BlinkLed_Get_List(void);
stLedConfig* BlinkLed_Get_idx(uint32_t);
void BlinkLed_RunTime(void *);
void BlinkLed_Init(void);

extern stLedConfig LED_List[LEDS_NUMBER];

#endif /* INCFILE1_H_ */
