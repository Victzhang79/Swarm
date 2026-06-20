package com.ruoyi.alarm.config;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.CorsRegistry;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;
import com.alibaba.fastjson.serializer.FastJsonConfig;
import com.alibaba.fastjson.serializer.SerializerFeature;
import com.alibaba.fastjson.spring.FastJsonHttpMessageConverter;
import com.ruoyi.alarm.interceptor.AppAuthInterceptor;

/**
 * 预警模块 WebMvc 配置
 * 注册 AppAuthInterceptor 拦截 /notify/** 并排除 /notify/callback/**（回调由各渠道签名校验）
 * 配置 FastJSON 消息转换器与跨域策略
 * 
 * @author ruoyi
 */
@Configuration
public class AlarmWebMvcConfig implements WebMvcConfigurer
{
    @Autowired
    private AppAuthInterceptor appAuthInterceptor;

    @Override
    public void addInterceptors(InterceptorRegistry registry)
    {
        registry.addInterceptor(appAuthInterceptor)
                .addPathPatterns("/notify/**")
                .excludePathPatterns("/notify/callback/**");
    }

    @Override
    public void addCorsMappings(CorsRegistry registry)
    {
        registry.addMapping("/**")
                .allowedOriginPatterns("*")
                .allowedMethods("GET", "POST", "PUT", "DELETE", "OPTIONS")
                .allowCredentials(true)
                .maxAge(3600);
    }
}
