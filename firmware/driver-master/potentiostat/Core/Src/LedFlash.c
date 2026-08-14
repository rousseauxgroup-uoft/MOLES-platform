/*
 * LedFlash.c
 *
 * Created: 25/02/2016 8:34:31
 *  Author: agarcia
 */ 

#include "user.h"

stLedConfig LED_List[LEDS_NUMBER] = {0};

void BlinkLed_Get_Default(stLedConfig *pLed)
{
	// Led flashing default configuring
	
	pLed->bits.Counter = 0;
	pLed->bits.InvertOut = 1;
	pLed->pin = 0xff;
	pLed->bits.LedState = 0;
	pLed->bits.Loop = 0;
	pLed->Off_Time = 200;
	pLed->On_Time = 20;
	pLed->StartTime = HAL_GetTick();
}

static void BlinkLed_Set_Output(void *p_out, uint32_t val)
{
	stLedConfig *pLed = (stLedConfig *)p_out;
	uint32_t *p_reg = (uint32_t *)pLed->port;

	*p_reg &= ~pLed->pin;
	*p_reg |= val ? pLed->pin : 0;
}

// Set flashing period (frequency) and duty (0 to period)

void BlinkLed_Set_Period(stLedConfig *pLed, uint32_t period, uint32_t duty)
{
	pLed->On_Time = duty;
	pLed->Off_Time = (period - duty);
}

void BlinkLed_Start(stLedConfig *pLed, uint32_t nTimes)
{
	if(!pLed->bits.Counter)
	{
		pLed->StartTime = HAL_GetTick();
		pLed->bits.LedState = 1;
		pLed->bits.Enable = 1;

		if(nTimes < BLINK_FOREVER)
			pLed->bits.Counter = nTimes;
		else
			pLed->bits.Loop = 1;
	}
}

void BlinkLed_Idx_Start(uint32_t index, uint32_t n_times)
{
	/* Check if index is less than LEDS_NUMBER otherwise return */
	if(index < LEDS_NUMBER)
		BlinkLed_Start(&LED_List[index], n_times);
}

void BlinkLed_Stop(stLedConfig *pLed)
{
	pLed->reg = 0;
	BlinkLed_Set_Output(pLed, 0);
}

stLedConfig* BlinkLed_Get_List(void)
{
	return LED_List;
}

stLedConfig* BlinkLed_Get_idx(uint32_t nLed)
{
	if(nLed < LEDS_NUMBER)
	{
		return &LED_List[nLed];
	}
	else return NULL;
}

void BlinkLed_Compute(stLedConfig *pLed)
{
	uint32_t PresentTm , EllapsedTm;

	if(!pLed->bits.Enable)
		return;

	if(pLed->bits.Counter || pLed->bits.Loop)
	{
		PresentTm = HAL_GetTick();
		EllapsedTm = TimeDiff(pLed->StartTime, PresentTm);
		
		if(pLed->bits.LedState)
		{
			if(EllapsedTm > pLed->On_Time)
			{
				
				pLed->StartTime = PresentTm;
				pLed->bits.LedState = 0;
			}
			else if(!pLed->bits.LedOut)
			{
				pLed->bits.LedOut = 1;

				if(pLed->out_led != NULL)
					pLed->out_led(pLed, !pLed->bits.InvertOut);
			}
		}
		else
		{
			if(EllapsedTm > pLed->Off_Time)
			{
				if(pLed->bits.Counter)
					pLed->bits.Counter--;

				pLed->StartTime = PresentTm;

				if(pLed->bits.Counter || pLed->bits.Loop)
					pLed->bits.LedState = 1;
			}
			else if(pLed->bits.LedOut)
			{
				pLed->bits.LedOut = 0;

				if(pLed->out_led != NULL)
					pLed->out_led(pLed, pLed->bits.InvertOut);
			}
		}
		
	}
}

void BlinkLed_RunTime(void *argument)
{
	while(1)
	{
		for(uint32_t x = 0; x < LEDS_NUMBER; x++)
			BlinkLed_Compute(&LED_List[x]);

		osDelay(1);	// Wait 1 ms
	}
}

void BlinkLed_Init(void)
{
	stLedConfig *p_led;

	for(uint32_t x = 0; x < LEDS_NUMBER; x++)
	{
		BlinkLed_Get_Default(&LED_List[x]);
		LED_List[x].out_led = BlinkLed_Set_Output;
	}

	p_led = &LED_List[LED_RED];
	p_led->port = (void *)&RED_LED_GPIO_Port->ODR;
	p_led->pin = RED_LED_Pin;
	BlinkLed_Set_Period(p_led, 100, 20);
	BlinkLed_Start(p_led, 10);

	p_led = &LED_List[LED_BLUE];
	p_led->port = (void *)&BLUE_LED_GPIO_Port->ODR;
	p_led->pin = BLUE_LED_Pin;
	BlinkLed_Set_Period(p_led, 100, 20);
	BlinkLed_Start(p_led, 10);

	p_led = &LED_List[LED_GREEN];
	p_led->port = (void *)&GREEN_LED_GPIO_Port->ODR;
	p_led->pin = GREEN_LED_Pin;
	BlinkLed_Set_Period(p_led, 1000, 20);
	BlinkLed_Start(p_led, BLINK_FOREVER);
}

