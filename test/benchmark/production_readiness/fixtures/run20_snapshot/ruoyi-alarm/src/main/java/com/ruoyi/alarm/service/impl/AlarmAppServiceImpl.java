package com.ruoyi.alarm.service.impl;

import java.util.List;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import com.ruoyi.alarm.domain.AlarmApp;
import com.ruoyi.alarm.mapper.AlarmAppMapper;
import com.ruoyi.alarm.service.IAlarmAppService;

/**
 * 预警应用 服务实现
 * 
 * @author ruoyi
 */
@Service
public class AlarmAppServiceImpl implements IAlarmAppService
{
    @Autowired
    private AlarmAppMapper alarmAppMapper;

    /**
     * 查询预警应用
     * 
     * @param appId 预警应用主键
     * @return 预警应用
     */
    @Override
    public AlarmApp selectAlarmAppById(Long appId)
    {
        return alarmAppMapper.selectAlarmAppById(appId);
    }

    /**
     * 根据应用ID查询预警应用
     * 
     * @param appId 应用ID
     * @return 预警应用
     */
    @Override
    public AlarmApp selectAlarmAppByAppId(Long appId)
    {
        return alarmAppMapper.selectAlarmAppByAppId(appId);
    }

    /**
     * 查询预警应用列表
     * 
     * @param alarmApp 预警应用
     * @return 预警应用
     */
    @Override
    public List<AlarmApp> selectAlarmAppList(AlarmApp alarmApp)
    {
        return alarmAppMapper.selectAlarmAppList(alarmApp);
    }

    /**
     * 新增预警应用
     * 
     * @param alarmApp 预警应用
     * @return 结果
     */
    @Override
    public int insertAlarmApp(AlarmApp alarmApp)
    {
        return alarmAppMapper.insertAlarmApp(alarmApp);
    }

    /**
     * 修改预警应用
     * 
     * @param alarmApp 预警应用
     * @return 结果
     */
    @Override
    public int updateAlarmApp(AlarmApp alarmApp)
    {
        return alarmAppMapper.updateAlarmApp(alarmApp);
    }

    /**
     * 批量删除预警应用
     * 
     * @param appIds 需要删除的预警应用主键
     * @return 结果
     */
    @Override
    public int deleteAlarmAppByIds(Long[] appIds)
    {
        return alarmAppMapper.deleteAlarmAppByIds(appIds);
    }

    /**
     * 删除预警应用
     * 
     * @param appId 预警应用主键
     * @return 结果
     */
    @Override
    public int deleteAlarmAppById(Long appId)
    {
        return alarmAppMapper.deleteAlarmAppById(appId);
    }

    /**
     * 校验应用身份
     * 
     * @param appId 应用ID
     * @param appSecret 应用秘钥
     * @return 结果
     */
    @Override
    public boolean validateApp(Long appId, String appSecret)
    {
        AlarmApp alarmApp = selectAlarmAppByAppId(appId);
        if (alarmApp == null)
        {
            return false;
        }
        return alarmApp.getAppSecret().equals(appSecret);
    }
}
